//! Compression: choosing a mode per block and framing the .paw file. A port of
//! python/src/paw/blocks.py and the writing side of stream.py, making the same choices.

use crate::container::{
    extension_allowed, Mode, DEFAULT_BLOCK_SIZE, FORMAT_MAX_BLOCK_SIZE, FORMAT_VERSION, MAGIC,
};
use crate::crc32::crc32;
use crate::error::Error;
use crate::mapcodec::encode_map;
use crate::series::python_sum;
use crate::transforms::{bwt_with_rows, delta, mtf, x86_call_count, x86_calls};
use crate::varint::write_varint;
use std::io::{Read, Write};

const ALL_MODES: [Mode; 7] = [
    Mode::Stored,
    Mode::Map,
    Mode::BwtMtfMap,
    Mode::DeltaMap,
    Mode::DeltaBwtMtfMap,
    Mode::BwtMap,
    Mode::DeltaBwtMap,
];
const BASIC_MODES: [Mode; 4] = [Mode::Stored, Mode::Map, Mode::BwtMtfMap, Mode::BwtMap];
const DELTA_DISTANCES: [usize; 4] = [1, 2, 3, 4];
/// The x86 mode is tried on blocks with at least one call the filter converts per this many
/// bytes: the benchmark executables have 4 to 14, other data at most about 0.35.
const X86_BYTES_PER_CALL: usize = 512;

const PROBE_MIN: usize = 256 * 1024;
const PROBE_SLICE: usize = 16 * 1024;
const PROBE_SLICES: usize = 4;
const PROBE_GIVE_UP: f64 = 0.95;
/// A mode without BWT is tried on the whole block only if its sample body is at most 15%
/// larger than with BWT (see the Python `blocks.py`).
const PROBE_SKIP_PERCENT: usize = 115;

/// How to compress (see the Python `paw.compress`).
#[derive(Clone, Debug)]
pub struct Options {
    pub block_size: u64,
    /// Modes to try for each block; None means automatic.
    pub modes: Option<Vec<Mode>>,
    /// The original file's extension (without the dot), stored in the header.
    pub extension: Option<String>,
    /// Try every mode even on blocks whose sample shows they are already compressed.
    pub thorough: bool,
    /// Blocks compressed at the same time (default 1: memory grows with each extra worker).
    pub workers: usize,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            block_size: DEFAULT_BLOCK_SIZE,
            modes: None,
            extension: None,
            thorough: false,
            workers: 1,
        }
    }
}

/// Order-0 entropy estimate in bytes; counts summed in first-seen order like Python.
fn entropy(data: &[u8]) -> f64 {
    let mut counts = [0u64; 256];
    let mut order = Vec::with_capacity(256);
    for &b in data {
        if counts[b as usize] == 0 {
            order.push(b);
        }
        counts[b as usize] += 1;
    }
    let n = data.len() as f64;
    python_sum(order.iter().map(|&b| {
        let c = counts[b as usize] as f64;
        c * (n / c).log2()
    })) / 8.0
}

/// (best delta distance, whether it looks better than no filter).
fn pick_delta(data: &[u8]) -> (usize, bool) {
    let mut best = (DELTA_DISTANCES[0], f64::INFINITY);
    for d in DELTA_DISTANCES {
        let score = entropy(&delta(data, d));
        if score < best.1 {
            best = (d, score);
        }
    }
    (best.0, best.1 < entropy(data))
}

/// True if `data` looks like x86 machine code: enough calls the x86 filter converts.
fn looks_like_x86(data: &[u8]) -> bool {
    x86_call_count(data) * X86_BYTES_PER_CALL >= data.len()
}

/// The BWT of a block's data, filtered or not: (last column, primary index, walk rows).
type Bwt = (Vec<u8>, usize, Vec<usize>);

/// The body in `mode` of `input`, the block's data (filtered for the delta and x86 modes);
/// `bwt` is the BWT of `input` for the BWT modes.
fn body(input: &[u8], mode: Mode, distance: usize, bwt: Option<&Bwt>) -> Vec<u8> {
    if mode == Mode::Stored {
        return input.to_vec();
    }
    let mut out = Vec::new();
    if mode.uses_delta() {
        write_varint(&mut out, distance as u64);
    }
    let Some((last, primary, rows)) = bwt else {
        out.extend(encode_map(input, false));
        return out;
    };
    write_varint(&mut out, *primary as u64);
    for &row in rows {
        write_varint(&mut out, row as u64);
    }
    if mode.uses_mtf() {
        out.extend(encode_map(&mtf(last), false));
    } else {
        out.extend(encode_map(last, true));
    }
    out
}

fn sample(data: &[u8]) -> Vec<u8> {
    let step = (data.len() - PROBE_SLICE) / (PROBE_SLICES - 1);
    (0..PROBE_SLICES)
        .flat_map(|i| &data[i * step..i * step + PROBE_SLICE])
        .copied()
        .collect()
}

/// Sizes of the sample's bodies in a mode without BWT and its BWT counterpart.
fn sample_sizes(sample: &[u8], pair: [Mode; 2], distance: usize) -> (usize, usize) {
    let sizes = bodies(sample, &pair, distance);
    (sizes[0].len(), sizes[1].len())
}

/// True if a sample body without BWT is too large to win on the whole block.
fn beaten((without, with_bwt): (usize, usize)) -> bool {
    100 * without > PROBE_SKIP_PERCENT * with_bwt
}

/// Encode one non-empty block in every candidate mode; return the smallest. With automatic
/// modes and a block of PROBE_MIN bytes or more, a sample decides first: an incompressible
/// block is stored, and a mode without BWT that its sample shows can't win is not tried.
pub fn compress_block(data: &[u8], modes: Option<&[Mode]>, thorough: bool) -> (Mode, Vec<u8>) {
    let mut distance = 0;
    let modes: Vec<Mode> = match modes {
        Some(m) => {
            if m.iter().any(|x| x.uses_delta()) {
                distance = pick_delta(data).0;
            }
            m.to_vec()
        }
        None => {
            let sample = (!thorough && data.len() >= PROBE_MIN).then(|| sample(data));
            let plain = sample
                .as_ref()
                .map(|s| sample_sizes(s, [Mode::Map, Mode::BwtMtfMap], 0));
            if let (Some(s), Some((a, b))) = (&sample, plain) {
                if a.min(b) as f64 >= PROBE_GIVE_UP * s.len() as f64 {
                    return (Mode::Stored, data.to_vec());
                }
            }
            let (d, worthwhile) = pick_delta(data);
            distance = d;
            let mut modes = if worthwhile {
                ALL_MODES.to_vec()
            } else {
                BASIC_MODES.to_vec()
            };
            if looks_like_x86(data) {
                modes.push(Mode::X86BwtMap);
            }
            if let (Some(s), Some(plain)) = (&sample, plain) {
                let delta_pair = [Mode::DeltaMap, Mode::DeltaBwtMtfMap];
                let skip_delta =
                    modes.contains(&Mode::DeltaMap) && beaten(sample_sizes(s, delta_pair, d));
                modes.retain(|&m| {
                    !(m == Mode::Map && beaten(plain) || m == Mode::DeltaMap && skip_delta)
                });
            }
            modes
        }
    };
    let bodies = bodies(data, &modes, distance);
    let mut best: Option<(Mode, Vec<u8>)> = None;
    for (&mode, b) in modes.iter().zip(bodies) {
        if best.as_ref().is_none_or(|(_, x)| b.len() < x.len()) {
            best = Some((mode, b)); // the first wins on ties
        }
    }
    best.unwrap()
}

/// The body of `data` in each of `modes`. The BWT modes on the same input (filtered or not)
/// form one group that sorts once and codes the BWT output with and without MTF; the groups,
/// and the modes within a group, run in parallel.
fn bodies(data: &[u8], modes: &[Mode], distance: usize) -> Vec<Vec<u8>> {
    let filtered = modes
        .iter()
        .any(|m| m.uses_delta())
        .then(|| delta(data, distance));
    let calls = modes.iter().any(|m| m.uses_x86()).then(|| {
        let mut calls = data.to_vec();
        x86_calls(&mut calls, true);
        calls
    });
    let input = |m: Mode| match (&filtered, &calls) {
        (Some(f), _) if m.uses_delta() => &f[..],
        (_, Some(c)) if m.uses_x86() => &c[..],
        _ => data,
    };
    let mut groups: Vec<Vec<usize>> = Vec::new(); // indices into `modes`
    for (i, &m) in modes.iter().enumerate() {
        let shares = |g: &&mut Vec<usize>| {
            let first = modes[g[0]];
            m.uses_bwt()
                && first.uses_bwt()
                && first.uses_delta() == m.uses_delta()
                && first.uses_x86() == m.uses_x86()
        };
        match groups.iter_mut().find(shares) {
            Some(g) => g.push(i),
            None => groups.push(vec![i]),
        }
    }
    let coded = each(&groups, |group| {
        let src = input(modes[group[0]]);
        let bwt = modes[group[0]].uses_bwt().then(|| bwt_with_rows(src));
        each(group, |&i| body(src, modes[i], distance, bwt.as_ref()))
    });
    let mut out = vec![Vec::new(); modes.len()];
    for (group, bodies) in groups.iter().zip(coded) {
        for (&i, b) in group.iter().zip(bodies) {
            out[i] = b;
        }
    }
    out
}

/// `f` of each item, computed in parallel threads where available (the items are
/// independent; WebAssembly in browsers has no threads by default).
fn each<T: Sync, R: Send>(items: &[T], f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    if cfg!(target_family = "wasm") || items.len() < 2 {
        return items.iter().map(&f).collect();
    }
    std::thread::scope(|scope| {
        let handles: Vec<_> = items.iter().map(|item| scope.spawn(|| f(item))).collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    })
}

pub fn encode_block(data: &[u8], modes: Option<&[Mode]>, thorough: bool) -> Vec<u8> {
    let (mode, body) = compress_block(data, modes, thorough);
    let mut out = Vec::with_capacity(body.len() + 16);
    write_varint(&mut out, data.len() as u64);
    out.push(mode as u8);
    out.extend(crc32(data).to_le_bytes());
    write_varint(&mut out, body.len() as u64);
    out.extend(body);
    out
}

pub fn header(block_size: u64, extension: Option<&str>) -> Vec<u8> {
    let mut out = MAGIC.to_vec();
    out.push(FORMAT_VERSION);
    write_varint(&mut out, block_size);
    let ext = extension.unwrap_or("").as_bytes();
    out.push(ext.len() as u8);
    out.extend(ext);
    write_varint(&mut out, 0); // no optional header data
    out
}

pub fn end(total: u64) -> Vec<u8> {
    let mut out = Vec::new();
    write_varint(&mut out, 0);
    write_varint(&mut out, total);
    out
}

/// Check options: block size in range, extension storable ("png" or ".png").
pub fn check_options(options: &Options) -> Result<Option<String>, String> {
    if !(1..=FORMAT_MAX_BLOCK_SIZE).contains(&options.block_size) {
        return Err(format!(
            "block_size must be between 1 and {FORMAT_MAX_BLOCK_SIZE} bytes"
        ));
    }
    if options.modes.as_ref().is_some_and(|m| m.is_empty()) {
        return Err("modes must not be empty".into());
    }
    match options
        .extension
        .as_deref()
        .map(|e| e.strip_prefix('.').unwrap_or(e))
    {
        None | Some("") => Ok(None),
        Some(e) if extension_allowed(e.as_bytes()) => Ok(Some(e.to_string())),
        Some(e) => Err(format!(
            "extension must be up to 16 letters, digits, - or _: {e:?}"
        )),
    }
}

/// Compress data into a complete .paw file.
pub fn compress(data: &[u8], options: &Options) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    compress_stream(data, &mut out, options, &mut |_| {}).map_err(|e| e.to_string())?;
    Ok(out)
}

/// Compress everything `src` yields into a .paw stream written to `dst`, a few blocks at a
/// time, so memory stays bounded by `workers` blocks. Returns the number of input bytes;
/// `progress` gets the input bytes compressed so far after each block, and once at the end.
pub fn compress_stream<R: Read, W: Write>(
    mut src: R,
    mut dst: W,
    options: &Options,
    progress: &mut dyn FnMut(u64),
) -> crate::Result<u64> {
    let extension = check_options(options).map_err(Error::Invalid)?;
    let io = |e: std::io::Error| Error::Io(e.to_string());
    dst.write_all(&header(options.block_size, extension.as_deref()))
        .map_err(io)?;
    let encode = |block: &[u8]| encode_block(block, options.modes.as_deref(), options.thorough);
    let workers = if cfg!(target_family = "wasm") {
        1
    } else {
        options.workers.max(1)
    };
    let mut total = 0u64;
    loop {
        let mut group = Vec::with_capacity(workers);
        while group.len() < workers {
            let mut block = Vec::new();
            (&mut src)
                .take(options.block_size)
                .read_to_end(&mut block)
                .map_err(io)?;
            if block.is_empty() {
                break;
            }
            group.push(block);
        }
        if group.is_empty() {
            break;
        }
        let framed: Vec<Vec<u8>> = if group.len() == 1 {
            vec![encode(&group[0])]
        } else {
            std::thread::scope(|scope| {
                let handles: Vec<_> = group
                    .iter()
                    .map(|b| scope.spawn(move || encode(b)))
                    .collect();
                handles.into_iter().map(|h| h.join().unwrap()).collect()
            })
        };
        for (block, f) in group.iter().zip(framed) {
            dst.write_all(&f).map_err(io)?;
            total += block.len() as u64;
            progress(total);
        }
    }
    dst.write_all(&end(total)).map_err(io)?;
    dst.flush().map_err(io)?;
    progress(total);
    Ok(total)
}
