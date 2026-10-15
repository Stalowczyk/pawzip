//! Finding series that describe a sorted list of positions (encoder side).
//!
//! A port of python/src/paw/series.py that makes exactly the same choices, so that both
//! encoders write identical files: candidate order, tie-breaking and the order in which
//! floating-point estimates are summed all follow the Python code.

use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::hash::{BuildHasherDefault, Hasher};

/// A fast, non-cryptographic hash for integer keys (the "Fx" hash used by rustc). The
/// default SipHash resists hash flooding, which doesn't matter for positions in a block.
#[derive(Default)]
pub struct FxHasher(u64);

impl Hasher for FxHasher {
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.write_u64(u64::from(b));
        }
    }

    fn write_u64(&mut self, n: u64) {
        self.0 = (self.0.rotate_left(5) ^ n).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
    }

    fn write_usize(&mut self, n: usize) {
        self.write_u64(n as u64);
    }

    fn finish(&self) -> u64 {
        self.0
    }
}

pub type HashMap<K, V> = std::collections::HashMap<K, V, BuildHasherDefault<FxHasher>>;
pub type HashSet<K> = std::collections::HashSet<K, BuildHasherDefault<FxHasher>>;

use crate::mapcodec::{Shape, ARITH, NESTED};
use crate::numbers::gap_class;

const MIN_CHAIN: u64 = 3;
const TOP_STEPS: usize = 24;
const TOP_STRIDES: usize = 8;
const NEIGHBOURS: usize = 4;

/// (kind, start, parameters): ARITH step, count; NESTED width, stride, rows, inner.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Series {
    pub kind: u64,
    pub start: u64,
    pub params: [u64; 4],
}

impl Series {
    fn arith(start: u64, step: u64, count: u64) -> Self {
        Series {
            kind: ARITH,
            start,
            params: [step, count, 0, 0],
        }
    }

    fn nested(start: u64, width: u64, stride: u64, rows: u64, inner: u64) -> Self {
        Series {
            kind: NESTED,
            start,
            params: [width, stride, rows, inner],
        }
    }

    pub fn shape(&self) -> Shape {
        Shape {
            kind: self.kind,
            params: self.params,
        }
    }

    /// Positions in the same order as Python's series_positions.
    pub fn positions(&self) -> impl Iterator<Item = u64> + '_ {
        let [a, b, c, d] = self.params;
        let (outer, inner_count) = if self.kind == ARITH { (1, b) } else { (c, a) };
        (0..outer).flat_map(move |r| {
            (0..inner_count).map(move |j| {
                if self.kind == ARITH {
                    self.start + j * a
                } else {
                    self.start + r * b + j * d
                }
            })
        })
    }

    fn values(&self) -> impl Iterator<Item = u64> + '_ {
        let count = if self.kind == ARITH { 2 } else { 4 };
        std::iter::once(self.start).chain(self.params[..count].iter().copied())
    }
}

/// Python's built-in sum() of floats (3.12+): Neumaier-compensated, starting from 0.
pub fn python_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    let (mut total, mut c) = (0.0f64, 0.0f64);
    for x in values {
        let t = total + x;
        if total.abs() >= x.abs() {
            c += (total - t) + x;
        } else {
            c += (x - t) + total;
        }
        total = t;
    }
    if c != 0.0 && c.is_finite() {
        total += c;
    }
    total
}

fn number_bits(value: u64) -> u64 {
    3 + u64::from(gap_class(value).1)
}

pub fn series_cost(s: &Series) -> f64 {
    (1 + s.values().map(number_bits).sum::<u64>()) as f64 / 8.0
}

pub fn reused_cost(s: &Series) -> f64 {
    (2 + number_bits(s.start)) as f64 / 8.0
}

fn gaps(positions: impl IntoIterator<Item = u64>) -> impl Iterator<Item = u64> {
    let mut prev: i64 = -1;
    positions.into_iter().map(move |p| {
        let g = (p as i64 - prev - 1) as u64;
        prev = p as i64;
        g
    })
}

/// Class counts in first-seen order (Python's Counter iterates in insertion order).
fn class_counts(gaps: impl Iterator<Item = u64>) -> (Vec<(usize, u64)>, u64) {
    let mut order: Vec<(usize, u64)> = Vec::new();
    let mut index = [usize::MAX; 64];
    let mut extra = 0;
    for g in gaps {
        let (cls, nbits, _) = gap_class(g);
        extra += u64::from(nbits);
        if index[cls] == usize::MAX {
            index[cls] = order.len();
            order.push((cls, 0));
        }
        order[index[cls]].1 += 1;
    }
    (order, extra)
}

pub fn estimate_bytes(gaps: impl Iterator<Item = u64>) -> f64 {
    let (counts, extra) = class_counts(gaps);
    let total: u64 = counts.iter().map(|c| c.1).sum();
    let bits = python_sum(
        counts
            .iter()
            .map(|&(_, c)| c as f64 * (total as f64 / c as f64).log2()),
    );
    (bits + extra as f64) / 8.0
}

/// A set of positions in `base..base + len`, one bit each: much faster than a hash set.
#[derive(Clone)]
struct Bits {
    base: u64,
    len: u64,
    words: Vec<u64>,
}

impl Bits {
    fn of(values: &[u64]) -> Self {
        let base = values.first().copied().unwrap_or(0);
        let len = values.last().map_or(0, |&l| l - base + 1);
        let mut bits = Bits {
            base,
            len,
            words: vec![0; (len as usize).div_ceil(64)],
        };
        for &v in values {
            bits.insert(v);
        }
        bits
    }

    #[inline]
    fn contains(&self, v: u64) -> bool {
        let Some(i) = v.checked_sub(self.base) else {
            return false;
        };
        i < self.len && self.words[(i >> 6) as usize] & (1 << (i & 63)) != 0
    }

    fn insert(&mut self, v: u64) {
        let i = v - self.base;
        self.words[(i >> 6) as usize] |= 1 << (i & 63);
    }

    fn remove(&mut self, v: u64) {
        let i = v - self.base;
        self.words[(i >> 6) as usize] &= !(1 << (i & 63));
    }
}

/// Estimated leftover cost of each position, looked up by its rank among the positions.
struct Weights<'a> {
    set: &'a Bits,
    /// Positions before each word of `set`.
    before: Vec<u32>,
    weight: Vec<f64>,
}

impl Weights<'_> {
    #[inline]
    fn of(&self, p: u64) -> f64 {
        let i = p - self.set.base;
        let w = (i >> 6) as usize;
        let below = self.set.words[w] & ((1u64 << (i & 63)) - 1);
        self.weight[(self.before[w] + below.count_ones()) as usize]
    }
}

/// `set` must hold exactly `positions`.
fn position_weights<'a>(positions: &[u64], set: &'a Bits) -> Weights<'a> {
    let info: Vec<(usize, u32)> = gaps(positions.iter().copied())
        .map(|g| {
            let (c, n, _) = gap_class(g);
            (c, n)
        })
        .collect();
    let mut counts = [0u64; 64];
    for &(c, _) in &info {
        counts[c] += 1;
    }
    let total = info.len() as f64;
    let weight = info
        .iter()
        .map(|&(c, n)| ((total / counts[c] as f64).log2() + f64::from(n)) / 8.0)
        .collect();
    let mut before = Vec::with_capacity(set.words.len());
    let mut count = 0u32;
    for w in &set.words {
        before.push(count);
        count += w.count_ones();
    }
    Weights {
        set,
        before,
        weight,
    }
}

/// Python: Counter of differences, most_common(limit + excluded), filtered.
fn top_diffs(values: &[u64], limit: usize, exclude_one: bool) -> Vec<u64> {
    let mut order: Vec<(u64, u64)> = Vec::new();
    let mut index: HashMap<u64, usize> = HashMap::default();
    let n = values.len();
    for j in 1..=NEIGHBOURS {
        for i in 0..n.saturating_sub(j) {
            let d = values[i + j] - values[i];
            let k = *index.entry(d).or_insert_with(|| {
                order.push((d, 0));
                order.len() - 1
            });
            order[k].1 += 1;
        }
    }
    order.sort_by_key(|&(_, count)| std::cmp::Reverse(count)); // stable: ties keep first-seen order
    let take = limit + usize::from(exclude_one);
    order
        .into_iter()
        .take(take)
        .filter(|&(d, c)| c >= 2 && !(exclude_one && d == 1))
        .map(|(d, _)| d)
        .take(limit)
        .collect()
}

/// `chains(values, values, ...)` for a sorted list, without a set: each value's successor
/// is found by a pointer moving along the list. (A bitset of nested starts would span the
/// whole block.)
fn chains_in_sorted(values: &[u64], step: u64, min_len: u64) -> Vec<(u64, u64)> {
    let mut next = vec![usize::MAX; values.len()];
    let mut has_previous = vec![false; values.len()];
    let mut j = 0;
    for (i, &v) in values.iter().enumerate() {
        while j < values.len() && values[j] < v + step {
            j += 1;
        }
        if j < values.len() && values[j] == v + step {
            next[i] = j;
            has_previous[j] = true;
        }
    }
    let mut out = Vec::new();
    for (i, &v) in values.iter().enumerate() {
        if has_previous[i] {
            continue;
        }
        let (mut n, mut at) = (1, i);
        while next[at] != usize::MAX {
            at = next[at];
            n += 1;
        }
        if n >= min_len {
            out.push((v, n));
        }
    }
    out
}

/// Maximal chains v, v+step, ... inside `set`, as (v, length), in `values` order.
fn chains(values: &[u64], set: &Bits, step: u64, min_len: u64) -> Vec<(u64, u64)> {
    let mut out = Vec::new();
    for &v in values {
        if v >= step && set.contains(v - step) {
            continue;
        }
        let mut n = 1;
        while set.contains(v + n * step) {
            n += 1;
        }
        if n >= min_len {
            out.push((v, n));
        }
    }
    out
}

/// Every candidate series, in the Python order, passed to `emit` as it is found (keeping
/// them all at once would take a lot of memory).
fn candidates(pos: &[u64], set: &Bits, bwt_output: bool, emit: &mut impl FnMut(Series)) {
    // On BWT output, chains with a step above 1 only link the first positions of runs (see
    // select_series).
    let run_starts: Vec<u64>;
    let run_start_set: Bits;
    let (linked, linked_set) = if bwt_output {
        run_starts = pos
            .iter()
            .copied()
            .filter(|&p| p == 0 || !set.contains(p - 1))
            .collect();
        run_start_set = Bits::of(&run_starts);
        (&run_starts[..], &run_start_set)
    } else {
        (pos, set)
    };
    let mut steps = vec![1];
    steps.extend(top_diffs(pos, TOP_STEPS, true));
    for &step in &steps {
        let (values, members) = if step == 1 {
            (pos, set)
        } else {
            (linked, linked_set)
        };
        for (v, n) in chains(values, members, step, MIN_CHAIN) {
            emit(Series::arith(v, step, n));
        }
    }
    for &inner in steps.iter().take(TOP_STRIDES + 1) {
        let (values, members) = if inner == 1 {
            (pos, set)
        } else {
            (linked, linked_set)
        };
        let mut by_width: Vec<(u64, Vec<u64>)> = Vec::new(); // insertion-ordered dict
        let mut index: HashMap<u64, usize> = HashMap::default();
        for (v, n) in chains(values, members, inner, 2) {
            let k = *index.entry(n).or_insert_with(|| {
                by_width.push((n, Vec::new()));
                by_width.len() - 1
            });
            by_width[k].1.push(v);
        }
        for (width, starts) in &by_width {
            if starts.len() < 2 {
                continue;
            }
            for stride in top_diffs(starts, TOP_STRIDES, false) {
                if stride <= (width - 1) * inner {
                    continue;
                }
                for (v, rows) in chains_in_sorted(starts, stride, 2) {
                    emit(Series::nested(v, *width, stride, rows, inner));
                }
            }
        }
    }
}

fn free_pieces(s: &Series, free: &Bits) -> Vec<Series> {
    let mut pieces = Vec::new();
    if s.kind == ARITH {
        let step = s.params[0];
        let mut run: Vec<u64> = Vec::new();
        for p in s.positions().map(Some).chain(std::iter::once(None)) {
            if let Some(p) = p {
                if free.contains(p) {
                    run.push(p);
                    continue;
                }
            }
            if run.len() as u64 >= MIN_CHAIN {
                pieces.push(Series::arith(run[0], step, run.len() as u64));
            }
            run.clear();
        }
        return pieces;
    }
    let [width, stride, rows, inner] = s.params;
    let mut group: Vec<u64> = Vec::new();
    for r in 0..=rows {
        let row_free =
            r < rows && (0..width).all(|j| free.contains(s.start + r * stride + j * inner));
        if row_free {
            group.push(r);
            continue;
        }
        if group.len() >= 2 {
            pieces.push(Series::nested(
                s.start + group[0] * stride,
                width,
                stride,
                group.len() as u64,
                inner,
            ));
        } else if group.len() == 1 && width >= MIN_CHAIN {
            pieces.push(Series::arith(s.start + group[0] * stride, inner, width));
        }
        group.clear();
    }
    pieces
}

/// Heap entry ordered like Python's (priority tuple, tie) in a min-heap.
struct Entry {
    not_run: bool,
    neg_gain: f64,
    tie: u64,
    series: Series,
}

impl Entry {
    fn key(&self) -> (bool, f64, u64) {
        (self.not_run, self.neg_gain, self.tie)
    }
}

impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Entry {}
impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Entry {
    fn cmp(&self, other: &Self) -> Ordering {
        // reversed: BinaryHeap is a max-heap, Python's heapq a min-heap
        let (a, b) = (self.key(), other.key());
        b.0.cmp(&a.0).then(b.1.total_cmp(&a.1)).then(b.2.cmp(&a.2))
    }
}

fn is_run(s: &Series) -> bool {
    s.kind == ARITH && s.params[0] == 1
}

fn greedy(
    positions: &[u64],
    members: &Bits,
    scored: &[(Series, f64)],
    savings: &dyn Fn(&Series) -> f64,
    protect_runs: bool,
) -> (Vec<Series>, Vec<u64>) {
    let entry = |s: Series, gain: f64, tie: u64| Entry {
        not_run: protect_runs && !is_run(&s),
        neg_gain: -gain,
        tie,
        series: s,
    };
    let mut heap: BinaryHeap<Entry> = scored
        .iter()
        .enumerate()
        .map(|(i, &(s, g))| entry(s, g, i as u64))
        .collect();
    let mut tie = heap.len() as u64;
    let mut free = members.clone();
    let mut chosen = Vec::new();
    while let Some(e) = heap.pop() {
        let s = e.series;
        if s.positions().all(|p| free.contains(p)) {
            for p in s.positions() {
                free.remove(p);
            }
            chosen.push(s);
            continue;
        }
        for piece in free_pieces(&s, &free) {
            let gain = savings(&piece);
            if gain > 0.0 {
                tie += 1;
                heap.push(entry(piece, gain, tie));
            }
        }
    }
    let leftovers = positions
        .iter()
        .copied()
        .filter(|&p| free.contains(p))
        .collect();
    (chosen, leftovers)
}

fn estimate(series: &[Series], leftovers: &[u64], known: &HashSet<Shape>) -> f64 {
    let mut seen: HashSet<Shape> = known.clone();
    let mut sorted = series.to_vec();
    sorted.sort_by_key(|s| s.start); // stable, like Python's sorted
    let mut total = 0.0f64;
    for s in &sorted {
        total += if seen.contains(&s.shape()) {
            reused_cost(s)
        } else {
            series_cost(s)
        };
        seen.insert(s.shape());
    }
    total + estimate_bytes(gaps(leftovers.iter().copied()))
}

/// Estimated bytes to store a series, cheaper if its shape is in `cheap`.
fn cost(s: &Series, cheap: &HashSet<Shape>) -> f64 {
    if cheap.contains(&s.shape()) {
        reused_cost(s)
    } else {
        series_cost(s)
    }
}

/// Split sorted positions into (series sorted by start, leftover positions). With
/// `bwt_output`, chains with a step above 1 only link the first positions of runs (see the
/// Python select_series).
pub fn select_series(
    positions: &[u64],
    known: &HashSet<Shape>,
    bwt_output: bool,
) -> (Vec<Series>, Vec<u64>) {
    if (positions.len() as u64) < MIN_CHAIN {
        return (Vec::new(), positions.to_vec());
    }
    let set = Bits::of(positions);
    let weight = position_weights(positions, &set);
    let mut best: (Vec<Series>, Vec<u64>) = (Vec::new(), positions.to_vec());
    let mut best_cost = estimate(&[], positions, known);
    // The candidates and the weight they cover are the same in both rounds. Keep those that
    // could save something with a remembered shape, the cheapest a series can be.
    let mut weighed: Vec<(Series, f64)> = Vec::new();
    candidates(positions, &set, bwt_output, &mut |s| {
        let covered = python_sum(s.positions().map(|p| weight.of(p)));
        if covered - reused_cost(&s) > 0.0 {
            weighed.push((s, covered));
        }
    });
    let mut cheap = known.clone();
    for _ in 0..2 {
        let savings = |s: &Series| -> f64 {
            python_sum(s.positions().map(|p| weight.of(p))) - cost(s, &cheap)
        };
        let scored: Vec<(Series, f64)> = weighed
            .iter()
            .filter_map(|&(s, covered)| {
                let g = covered - cost(&s, &cheap);
                (g > 0.0).then_some((s, g))
            })
            .collect();
        for protect_runs in [false, true] {
            let (chosen, leftovers) = greedy(positions, &set, &scored, &savings, protect_runs);
            let total = estimate(&chosen, &leftovers, known);
            if !chosen.is_empty() && total < best_cost {
                let mut sorted = chosen;
                sorted.sort_by_key(|s| s.start);
                best = (sorted, leftovers);
                best_cost = total;
            }
        }
        let more: HashSet<Shape> = cheap
            .iter()
            .copied()
            .chain(best.0.iter().map(Series::shape))
            .collect();
        if more.len() == cheap.len() {
            break;
        }
        cheap = more;
    }
    best
}
