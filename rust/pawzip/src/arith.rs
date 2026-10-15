//! Adaptive range coding (32-bit, byte-wise, LZMA style) plus a separate stream of raw bits,
//! as in spec/FORMAT.md. Every constant here is part of the format.

use crate::error::{format, Result};

pub const NUM_CLASSES: usize = 64;
const INCREMENT: u32 = 64;
const LIMIT: u32 = 1 << 14;

pub const BIT_ONE: u32 = 1 << 12;
const BIT_SHIFT: u32 = 6;

/// Adaptive frequency table over 64 symbols.
#[derive(Clone)]
pub struct Model {
    freq: [u32; NUM_CLASSES],
    total: u32,
}

impl Model {
    pub fn new() -> Self {
        Model {
            freq: [1; NUM_CLASSES],
            total: NUM_CLASSES as u32,
        }
    }

    pub fn with_freq(freq: [u32; NUM_CLASSES]) -> Self {
        Model {
            freq,
            total: freq.iter().sum(),
        }
    }

    pub fn range_of(&self, sym: usize) -> (u32, u32) {
        let low: u32 = self.freq[..sym].iter().sum();
        (low, low + self.freq[sym])
    }

    fn find(&self, count: u64) -> (usize, u32, u32) {
        let mut low = 0u32;
        for (sym, &f) in self.freq.iter().enumerate() {
            if count < u64::from(low + f) {
                return (sym, low, low + f);
            }
            low += f;
        }
        unreachable!("count is always below the total")
    }

    pub fn update(&mut self, sym: usize) {
        self.freq[sym] += INCREMENT;
        self.total += INCREMENT;
        if self.total > LIMIT {
            for f in &mut self.freq {
                *f = f.div_ceil(2);
            }
            self.total = self.freq.iter().sum();
        }
    }

    pub fn total(&self) -> u32 {
        self.total
    }
}

impl Default for Model {
    fn default() -> Self {
        Self::new()
    }
}

/// Adaptive probability of a single bit being 0, in units of 1/BIT_ONE.
#[derive(Clone, Copy)]
pub struct BitModel {
    pub p: u32,
}

impl BitModel {
    pub const fn new() -> Self {
        BitModel { p: BIT_ONE / 2 }
    }

    pub fn update(&mut self, bit: u32) {
        if bit != 0 {
            self.p -= self.p >> BIT_SHIFT;
        } else {
            self.p += (BIT_ONE - self.p) >> BIT_SHIFT;
        }
    }
}

impl Default for BitModel {
    fn default() -> Self {
        Self::new()
    }
}

/// Below this, the coder moves a byte out.
const RANGE_BOTTOM: u32 = 1 << 24;

/// Drop trailing zero bytes: decoders read zeros past the end of each stream.
fn trim(mut data: Vec<u8>) -> Vec<u8> {
    while data.last() == Some(&0) {
        data.pop();
    }
    data
}

/// Reads a coded stream: varint length of the range-coded bytes, those bytes, then raw bits.
pub struct Decoder<'a> {
    data: &'a [u8], // range-coded part
    pos: usize,
    range: u32,
    code: u32,
    raw: &'a [u8],
    raw_pos: u64,
}

impl<'a> Decoder<'a> {
    pub fn new(body: &'a [u8]) -> Result<Self> {
        let mut r = crate::varint::Reader::new(body);
        let size = r.varint()?;
        let start = r.pos;
        if size > (body.len() - start) as u64 {
            return format("range-coded part longer than the map body");
        }
        let end = start + size as usize;
        let mut d = Decoder {
            data: &body[start..end],
            pos: 0,
            range: u32::MAX,
            code: 0,
            raw: &body[end..],
            raw_pos: 0,
        };
        for _ in 0..4 {
            d.code = (d.code << 8) | u32::from(d.next_byte());
        }
        Ok(d)
    }

    #[inline]
    fn next_byte(&mut self) -> u8 {
        let b = self.data.get(self.pos).copied().unwrap_or(0);
        self.pos += 1;
        b
    }

    #[inline]
    fn normalize(&mut self) {
        while self.range < RANGE_BOTTOM {
            self.range <<= 8;
            self.code = (self.code << 8) | u32::from(self.next_byte());
        }
    }

    pub fn decode(&mut self, model: &mut Model) -> usize {
        let total = model.total;
        let r = self.range / total;
        let count = (self.code / r).min(total - 1); // only damaged data goes past the end
        let (sym, lo, hi) = model.find(u64::from(count));
        self.code = self.code.wrapping_sub(r * lo);
        self.range = r * (hi - lo);
        self.normalize();
        model.update(sym);
        sym
    }

    /// A raw bit, from the separate stream.
    #[inline]
    pub fn decode_bit(&mut self) -> u32 {
        let i = self.raw_pos;
        self.raw_pos += 1;
        match self.raw.get((i >> 3) as usize) {
            Some(&byte) => u32::from((byte >> (7 - (i & 7))) & 1),
            None => 0,
        }
    }

    pub fn decode_adaptive_bit(&mut self, model: &mut BitModel) -> u32 {
        let bound = (self.range >> 12) * model.p;
        let bit = if self.code < bound {
            self.range = bound;
            0
        } else {
            self.code -= bound;
            self.range -= bound;
            1
        };
        self.normalize();
        model.update(bit);
        bit
    }
}

/// Writes a coded stream (see Decoder).
pub struct Encoder {
    low: u64, // may reach 2^32: the carry goes into the bytes still waiting

    range: u32,
    cache: u8,       // the last byte not yet written (a carry may still change it)
    cache_size: u64, // that byte plus the 0xFF bytes after it, also waiting
    out: Vec<u8>,
    raw: Vec<u8>,
    raw_bits: u64,
}

impl Encoder {
    pub fn new() -> Self {
        Encoder {
            low: 0,
            range: u32::MAX,
            cache: 0,
            cache_size: 1,
            out: Vec::new(),
            raw: Vec::new(),
            raw_bits: 0,
        }
    }

    fn shift_low(&mut self) {
        if (self.low as u32) < 0xFF00_0000 || (self.low >> 32) != 0 {
            let carry = (self.low >> 32) as u8;
            let mut temp = self.cache;
            loop {
                self.out.push(temp.wrapping_add(carry));
                temp = 0xFF;
                self.cache_size -= 1;
                if self.cache_size == 0 {
                    break;
                }
            }
            self.cache = ((self.low as u32) >> 24) as u8;
        }
        self.cache_size += 1;
        self.low = u64::from(self.low as u32) << 8 & 0xFFFF_FFFF;
    }

    fn normalize(&mut self) {
        while self.range < RANGE_BOTTOM {
            self.range <<= 8;
            self.shift_low();
        }
    }

    pub fn encode(&mut self, model: &mut Model, sym: usize) {
        let (lo, hi) = model.range_of(sym);
        let r = self.range / model.total;
        self.low += u64::from(r * lo);
        self.range = r * (hi - lo);
        self.normalize();
        model.update(sym);
    }

    pub fn encode_bit(&mut self, bit: u32) {
        if self.raw_bits & 7 == 0 {
            self.raw.push(0);
        }
        if bit != 0 {
            *self.raw.last_mut().unwrap() |= 0x80 >> (self.raw_bits & 7);
        }
        self.raw_bits += 1;
    }

    pub fn encode_adaptive_bit(&mut self, model: &mut BitModel, bit: u32) {
        let bound = (self.range >> 12) * model.p;
        if bit == 0 {
            self.range = bound;
        } else {
            self.low += u64::from(bound);
            self.range -= bound;
        }
        self.normalize();
        model.update(bit);
    }

    pub fn finish(mut self) -> Vec<u8> {
        for _ in 0..5 {
            self.shift_low();
        }
        self.out.remove(0); // the first byte is always 0
        let coded = trim(self.out);
        let mut body = Vec::new();
        crate::varint::write_varint(&mut body, coded.len() as u64);
        body.extend(coded);
        body.extend(trim(self.raw));
        body
    }
}

impl Default for Encoder {
    fn default() -> Self {
        Self::new()
    }
}
