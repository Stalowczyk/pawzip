//! The free slots of a map body: which positions no byte value has claimed yet, with
//! "rank" (free slots before a position) and "select" (position of the k-th free slot).
//!
//! One bit per position. Ranks and selects come in ascending batches (all leftovers of one
//! byte value), so one sequential sweep over the words answers a whole batch: at most n / 64
//! word reads per byte value, against about 18 random reads per position in a tree.

pub struct FreeSlots {
    words: Vec<u64>, // bit set = free
    count: u64,
}

impl FreeSlots {
    pub fn new(n: usize) -> Self {
        let nwords = n.div_ceil(64);
        let mut words = vec![u64::MAX; nwords];
        if !n.is_multiple_of(64) {
            words[nwords - 1] = (1u64 << (n % 64)) - 1;
        }
        FreeSlots {
            words,
            count: n as u64,
        }
    }

    #[inline]
    pub fn is_free(&self, p: usize) -> bool {
        self.words[p >> 6] & (1 << (p & 63)) != 0
    }

    pub fn count(&self) -> u64 {
        self.count
    }

    /// Mark a free position as taken.
    #[inline]
    pub fn take(&mut self, p: usize) {
        debug_assert!(self.is_free(p));
        self.words[p >> 6] &= !(1 << (p & 63));
        self.count -= 1;
    }

    /// Number of free positions before each of `positions` (ascending).
    pub fn ranks(&self, positions: &[u64]) -> Vec<u64> {
        let (mut word, mut before) = (0usize, 0u64);
        positions
            .iter()
            .map(|&p| {
                let p = p as usize;
                while word < p >> 6 {
                    before += u64::from(self.words[word].count_ones());
                    word += 1;
                }
                let below = (1u64 << (p & 63)) - 1;
                before + u64::from((self.words[word] & below).count_ones())
            })
            .collect()
    }

    /// Position of the free slot with each rank in `ranks` (ascending, all below count()).
    pub fn select(&self, ranks: &[u64]) -> Vec<usize> {
        let (mut word, mut before) = (0usize, 0u64);
        ranks
            .iter()
            .map(|&k| {
                loop {
                    let ones = u64::from(self.words[word].count_ones());
                    if k < before + ones {
                        break;
                    }
                    before += ones;
                    word += 1;
                }
                word * 64 + select_in_word(self.words[word], (k - before) as u32)
            })
            .collect()
    }
}

/// Index of the set bit with rank `k` in `w` (which has more than k set bits).
#[inline]
fn select_in_word(mut w: u64, mut k: u32) -> usize {
    let mut base = 0;
    loop {
        let ones = (w & 0xff).count_ones();
        if k < ones {
            break;
        }
        k -= ones;
        w >>= 8;
        base += 8;
    }
    for _ in 0..k {
        w &= w - 1; // drop the lowest set bit
    }
    base + w.trailing_zeros() as usize
}

#[cfg(test)]
mod tests {
    use super::FreeSlots;

    #[test]
    fn rank_and_select_agree_with_a_plain_scan() {
        for n in [1, 63, 64, 65, 1000, 4096] {
            let mut slots = FreeSlots::new(n);
            let mut plain = vec![true; n];
            for p in (0..n).filter(|p| p % 3 == 0 || p % 7 == 2) {
                slots.take(p);
                plain[p] = false;
            }
            let free: Vec<usize> = (0..n).filter(|&p| plain[p]).collect();
            assert_eq!(slots.count(), free.len() as u64);
            let all: Vec<u64> = (0..free.len() as u64).collect();
            assert_eq!(slots.select(&all), free, "n={n}");
            let positions: Vec<u64> = free.iter().map(|&p| p as u64).collect();
            assert_eq!(slots.ranks(&positions), all, "n={n}");
            let some: Vec<u64> = all.iter().copied().filter(|k| k % 5 == 1).collect();
            let picked: Vec<usize> = some.iter().map(|&k| free[k as usize]).collect();
            assert_eq!(slots.select(&some), picked, "n={n}");
            for (p, &is_free) in plain.iter().enumerate() {
                assert_eq!(slots.is_free(p), is_free);
            }
        }
    }
}
