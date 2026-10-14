//! Numbers: a size class (adaptive model) plus extra bits, the first PREDICTED_BITS of them
//! coded with bit models; and the geometric prior for gap models. See spec/FORMAT.md.

use crate::arith::{BitModel, Decoder, Encoder, Model, NUM_CLASSES};

pub const PREDICTED_BITS: u32 = 4;
const NODES: usize = 1 << PREDICTED_BITS; // tree nodes 1..15 for the predicted bits

/// Bit models for the leading extra bits, by (class, node in the bit tree).
pub struct Contexts(Box<[BitModel; NUM_CLASSES * NODES]>);

impl Contexts {
    pub fn new() -> Self {
        Contexts(Box::new([BitModel::new(); NUM_CLASSES * NODES]))
    }

    fn at(&mut self, cls: usize, node: usize) -> &mut BitModel {
        &mut self.0[cls * NODES + node]
    }
}

impl Default for Contexts {
    fn default() -> Self {
        Self::new()
    }
}

/// (class, number of extra bits, value of the extra bits) for a number.
pub fn gap_class(value: u64) -> (usize, u32, u64) {
    let v = value + 1;
    if v < 4 {
        return ((v - 1) as usize, 0, 0);
    }
    let k = 63 - v.leading_zeros();
    let half = (v >> (k - 1)) & 1;
    (
        (2 * k - 1) as usize + half as usize,
        k - 1,
        v & ((1 << (k - 1)) - 1),
    )
}

/// (smallest number of the class, number of extra bits).
pub fn class_base(cls: usize) -> (u64, u32) {
    if cls < 3 {
        return (cls as u64, 0);
    }
    let k = (cls as u32).div_ceil(2);
    let half = cls as u64 - (2 * u64::from(k) - 1);
    (((1u64 << k) | (half << (k - 1))) - 1, k - 1)
}

pub fn get(dec: &mut Decoder, model: &mut Model, contexts: &mut Contexts) -> u64 {
    let cls = dec.decode(model);
    let (base, nbits) = class_base(cls);
    let mut extra = 0u64;
    let mut node = 1usize;
    for i in 0..nbits {
        let bit = if i < PREDICTED_BITS {
            let bit = dec.decode_adaptive_bit(contexts.at(cls, node));
            node = 2 * node + bit as usize;
            bit
        } else {
            dec.decode_bit()
        };
        extra = (extra << 1) | u64::from(bit);
    }
    base + extra
}

pub fn put(enc: &mut Encoder, model: &mut Model, contexts: &mut Contexts, value: u64) {
    let (cls, nbits, extra) = gap_class(value);
    enc.encode(model, cls);
    let mut node = 1usize;
    for i in 0..nbits {
        let bit = ((extra >> (nbits - 1 - i)) & 1) as u32;
        if i < PREDICTED_BITS {
            enc.encode_adaptive_bit(contexts.at(cls, node), bit);
            node = 2 * node + bit as usize;
        } else {
            enc.encode_bit(bit);
        }
    }
}

const ONE: u64 = 1 << 31; // Q31 fixed point
const PRIOR_WEIGHT: u64 = 1024;

fn fixed_pow(mut base: u64, mut exp: u64) -> u64 {
    let mut result = ONE;
    while exp != 0 {
        if exp & 1 != 0 {
            result = (result * base) >> 31;
        }
        base = (base * base) >> 31;
        exp >>= 1;
    }
    result
}

/// Starting class frequencies for a symbol with `k` leftovers among `n` free slots.
pub fn geometric_prior(n: u64, k: u64) -> [u32; NUM_CLASSES] {
    let r = ((n - k) << 31).checked_div(n).unwrap_or(ONE);
    let mut freq = [0u32; NUM_CLASSES];
    for (cls, f) in freq.iter_mut().enumerate() {
        let (base, nbits) = class_base(cls);
        let mass = fixed_pow(r, base).saturating_sub(fixed_pow(r, base + (1 << nbits)));
        *f = 1 + ((PRIOR_WEIGHT * mass) >> 31) as u32;
    }
    freq
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes_round_trip() {
        for v in (0..5000u64).chain([u64::from(u32::MAX) - 1, (1 << 32) + 7]) {
            let (cls, nbits, extra) = gap_class(v);
            assert_eq!(class_base(cls), (v - extra, nbits));
        }
    }

    #[test]
    fn prior_matches_python_table() {
        // pinned in python/tests/test_components.py::test_geometric_prior_known_table
        assert_eq!(
            geometric_prior(1000, 10)[..12],
            [11, 11, 11, 20, 20, 38, 37, 69, 63, 112, 95, 150]
        );
    }
}
