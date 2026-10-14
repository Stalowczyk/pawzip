//! The map body: a background byte, then one range-coded stream holding, for every
//! other byte value, whether it occurs, its series, and its leftover positions as gaps
//! counted in free slots. Series shapes are remembered (spec: "Remembered shapes").

use crate::arith::{Decoder, Encoder, Model, NUM_CLASSES};
use crate::error::{format, Result};
use crate::freeslots::FreeSlots;
use crate::numbers::{geometric_prior, get, put, Contexts};
use crate::series::{select_series, HashSet};

pub const ARITH: u64 = 0;
pub const NESTED: u64 = 1;
pub const RECENT_SHAPES: usize = NUM_CLASSES - 1;

/// A series without its start: (type, parameters...). ARITH: step, count.
/// NESTED: width, stride, rows, inner.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub struct Shape {
    pub kind: u64,
    pub params: [u64; 4],
}

fn param_count(kind: u64) -> usize {
    if kind == ARITH {
        2
    } else {
        4
    }
}

/// A coded number's model and the bit contexts of its extra bits.
#[derive(Default)]
pub(crate) struct NumberModel {
    pub model: Model,
    pub bits: Contexts,
}

#[derive(Default)]
pub(crate) struct Models {
    pub present: NumberModel,
    pub series_count: NumberModel,
    pub shape: Model,
    pub series_type: NumberModel,
    pub start: NumberModel,
    pub params: [[NumberModel; 4]; 2],
    pub leftover_count: NumberModel,
    pub gap_bits: Contexts,
}

fn number(dec: &mut Decoder, m: &mut NumberModel) -> u64 {
    get(dec, &mut m.model, &mut m.bits)
}

pub(crate) fn remember(recent: &mut Vec<Shape>, shape: Shape) {
    if let Some(i) = recent.iter().position(|s| *s == shape) {
        recent.remove(i);
    }
    recent.insert(0, shape);
    recent.truncate(RECENT_SHAPES);
}

fn read_shape(dec: &mut Decoder, m: &mut Models) -> Result<Shape> {
    let kind = number(dec, &mut m.series_type);
    if kind > NESTED {
        return format(format!("unknown series type {kind}"));
    }
    let mut params = [0u64; 4];
    for (i, p) in params.iter_mut().enumerate().take(param_count(kind)) {
        *p = number(dec, &mut m.params[kind as usize][i]);
    }
    if kind == ARITH {
        let [step, count, ..] = params;
        if step < 1 || count < 2 {
            return format("invalid arithmetic series");
        }
    } else {
        let [width, stride, rows, inner] = params;
        if width < 2
            || rows < 2
            || inner < 1
            || u128::from(stride) <= u128::from(width - 1) * u128::from(inner)
        {
            return format("invalid nested series");
        }
    }
    Ok(Shape { kind, params })
}

/// The highest position a series covers (computed without overflow).
fn last_position(start: u64, s: &Shape) -> u128 {
    let p = s.params.map(u128::from);
    if s.kind == ARITH {
        start as u128 + p[0] * (p[1] - 1)
    } else {
        start as u128 + p[1] * (p[2] - 1) + p[3] * (p[0] - 1)
    }
}

pub fn decode_map(body: &[u8], n: usize) -> Result<Vec<u8>> {
    let Some((&background, coded)) = body.split_first() else {
        return format("unexpected end of data");
    };
    let mut dec = Decoder::new(coded)?;
    let mut m = Models::default();
    let mut recent: Vec<Shape> = Vec::new();
    let mut out = vec![background; n];
    let mut free = FreeSlots::new(n);
    for b in 0..=255u8 {
        if b == background {
            continue;
        }
        let present = number(&mut dec, &mut m.present);
        if present > 1 {
            return format("invalid presence flag");
        }
        if present == 0 {
            continue;
        }
        let series_count = number(&mut dec, &mut m.series_count);
        let mut prev_start = 0u64;
        for _ in 0..series_count {
            let index = dec.decode(&mut m.shape);
            let shape = if index == RECENT_SHAPES {
                read_shape(&mut dec, &mut m)?
            } else if index < recent.len() {
                recent[index]
            } else {
                return format("series shape index refers to no remembered shape");
            };
            remember(&mut recent, shape);
            let start = prev_start.checked_add(number(&mut dec, &mut m.start));
            let Some(start) = start else {
                return format("series outside the data");
            };
            prev_start = start;
            if last_position(start, &shape) >= n as u128 {
                return format("series outside the data");
            }
            let mut place = |p: usize| -> Result<()> {
                if !free.is_free(p) {
                    return format("series overlap positions already taken");
                }
                free.take(p);
                out[p] = b;
                Ok(())
            };
            let s = start as usize;
            let [a, c, d, e] = shape.params.map(|x| x as usize);
            if shape.kind == ARITH {
                for i in 0..c {
                    place(s + i * a)?;
                }
            } else {
                for r in 0..d {
                    for j in 0..a {
                        place(s + r * c + j * e)?;
                    }
                }
            }
        }

        let leftovers = number(&mut dec, &mut m.leftover_count);
        if leftovers == 0 {
            if series_count == 0 {
                return format("byte value marked present without any positions");
            }
            continue;
        }
        let free_count = free.count();
        if leftovers > free_count {
            return format("more positions than free slots");
        }
        let prior = geometric_prior(free_count, leftovers);
        let mut models = [Model::with_freq(prior), Model::with_freq(prior)];
        let mut after_zero = false;
        let mut ranks = Vec::with_capacity(leftovers as usize);
        let mut r: i64 = -1;
        for _ in 0..leftovers {
            let model = &mut models[usize::from(after_zero)];
            let gap = get(&mut dec, model, &mut m.gap_bits);
            if gap >= free_count {
                return format("leftover position outside the data");
            }
            r += gap as i64 + 1;
            if r as u64 >= free_count {
                return format("leftover position outside the data");
            }
            ranks.push(r as u64);
            after_zero = gap == 0;
        }
        // ranks count the free slots before this byte's leftovers are placed
        let taken = free.select(&ranks);
        for p in taken {
            out[p] = b;
            free.take(p);
        }
    }
    Ok(out)
}

// --- encoding -----------------------------------------------------------------------

fn put_number(enc: &mut Encoder, m: &mut NumberModel, value: u64) {
    put(enc, &mut m.model, &mut m.bits, value);
}

/// The map body of `data`: background byte, then the coded stream (same choices as Python).
/// `bwt_output` says the data is BWT output itself (modes 5 and 6), which changes how series
/// are searched for (see `select_series`), not the format.
pub fn encode_map(data: &[u8], bwt_output: bool) -> Vec<u8> {
    let n = data.len();
    let mut positions: Vec<Vec<u64>> = vec![Vec::new(); 256];
    for (i, &b) in data.iter().enumerate() {
        positions[b as usize].push(i as u64);
    }
    let mut background = 0usize; // the most frequent byte value; the smallest on ties
    for b in 1..256 {
        if positions[b].len() > positions[background].len() {
            background = b;
        }
    }
    let mut enc = Encoder::new();
    let mut m = Models::default();
    let mut recent: Vec<Shape> = Vec::new();
    let mut free = FreeSlots::new(n);
    for (b, pos) in positions.iter().enumerate() {
        if b == background {
            continue;
        }
        put_number(&mut enc, &mut m.present, u64::from(!pos.is_empty()));
        if pos.is_empty() {
            continue;
        }
        let known: HashSet<Shape> = recent.iter().copied().collect();
        let (series, leftovers) = select_series(pos, &known, bwt_output);
        put_number(&mut enc, &mut m.series_count, series.len() as u64);
        let mut prev_start = 0;
        for s in &series {
            let shape = s.shape();
            match recent.iter().position(|r| *r == shape) {
                Some(i) => enc.encode(&mut m.shape, i),
                None => {
                    enc.encode(&mut m.shape, RECENT_SHAPES);
                    put_number(&mut enc, &mut m.series_type, s.kind);
                    for (i, &v) in s.params.iter().enumerate().take(param_count(s.kind)) {
                        put_number(&mut enc, &mut m.params[s.kind as usize][i], v);
                    }
                }
            }
            put_number(&mut enc, &mut m.start, s.start - prev_start);
            prev_start = s.start;
            remember(&mut recent, shape);
            for p in s.positions() {
                free.take(p as usize);
            }
        }
        put_number(&mut enc, &mut m.leftover_count, leftovers.len() as u64);
        if leftovers.is_empty() {
            continue;
        }
        let ranks = free.ranks(&leftovers);
        let prior = geometric_prior(free.count(), leftovers.len() as u64);
        // GAP0 for the first gap and after one above 0, GAP1 after a gap of 0 (spec)
        let mut models = [Model::with_freq(prior), Model::with_freq(prior)];
        let mut after_zero = false;
        let mut prev: i64 = -1;
        for &r in &ranks {
            let gap = (r as i64 - prev - 1) as u64;
            put(
                &mut enc,
                &mut models[usize::from(after_zero)],
                &mut m.gap_bits,
                gap,
            );
            after_zero = gap == 0;
            prev = r as i64;
        }
        for &p in &leftovers {
            free.take(p as usize);
        }
    }
    let mut out = vec![background as u8];
    out.extend(enc.finish());
    out
}
