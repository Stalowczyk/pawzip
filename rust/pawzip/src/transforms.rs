//! Reversible transforms applied before the map: BWT, move-to-front and delta.

/// Burrows-Wheeler transform with an end marker: (last column, primary index), as in the
/// spec. The suffixes of the input followed by a marker smaller than every byte are sorted;
/// the marker's own entry is left out, and the primary index (1..=n) is its row.
pub fn bwt(data: &[u8]) -> (Vec<u8>, usize) {
    let (last, primary, _) = bwt_with_rows(data);
    (last, primary)
}

/// Stored walk rows: one per WALK_UNIT bytes of a BWT block, up to MAX_WALKS (spec: "Walk
/// rows"). They let a decoder run several independent walks at once.
pub const WALK_UNIT: usize = 1 << 17;
pub const MAX_WALKS: usize = 16;

/// Number of walks K for a BWT block of n bytes (K - 1 rows are stored).
pub fn walk_count(n: usize) -> usize {
    (n / WALK_UNIT).clamp(1, MAX_WALKS)
}

/// The BWT plus the walk rows for the block (see the spec).
///
/// Sorts the suffixes with divsufsort; inputs too large for its 32-bit indices fall back to
/// prefix doubling.
pub fn bwt_with_rows(data: &[u8]) -> (Vec<u8>, usize, Vec<usize>) {
    if data.is_empty() {
        (Vec::new(), 0, Vec::new())
    } else if data.len() < 1 << 31 {
        let sa = divsufsort::sort(data).into_parts().1;
        from_suffix_array(data, sa.iter().map(|&s| s as usize))
    } else {
        let sa = suffix_array_by_doubling(data);
        from_suffix_array(data, sa.iter().map(|&s| s as usize))
    }
}

/// The BWT and walk rows read off the sorted suffixes of non-empty `data`. Row 0 of the table
/// is the marker alone, so suffix `sa[k]` sits in row k + 1.
fn from_suffix_array(data: &[u8], sa: impl Iterator<Item = usize>) -> (Vec<u8>, usize, Vec<usize>) {
    let n = data.len();
    let k = walk_count(n);
    let length = n.div_ceil(k);
    let mut last = Vec::with_capacity(n);
    last.push(data[n - 1]); // before the marker alone
    let mut primary = 0;
    let mut rows = vec![0; k - 1];
    for (row, s) in sa.enumerate() {
        if s == 0 {
            primary = row + 1; // the marker's entry, not stored
        } else {
            last.push(data[s - 1]);
            if s % length == 0 {
                rows[s / length - 1] = row + 1;
            }
        }
    }
    (last, primary, rows)
}

/// Sorted suffixes by prefix doubling (a suffix sorts before longer ones it is a prefix of):
/// the reference for tests and the fallback for blocks of 2 GiB or more.
pub fn suffix_array_by_doubling(data: &[u8]) -> Vec<u32> {
    let n = data.len();
    let mut rank: Vec<u32> = data.iter().map(|&b| u32::from(b)).collect();
    let mut order: Vec<u32> = (0..n as u32).collect();
    let mut keys: Vec<u64> = vec![0; n];
    let mut k = 1usize;
    if n < 2 {
        return order;
    }
    loop {
        for i in 0..n {
            // 0 = past the end, below every rank
            let second = if i + k < n {
                u64::from(rank[i + k]) + 1
            } else {
                0
            };
            keys[i] = (u64::from(rank[i]) << 32) | second;
        }
        order.sort_unstable_by_key(|&i| keys[i as usize]);
        let mut r = 0u32;
        rank[order[0] as usize] = 0;
        for w in 1..n {
            if keys[order[w] as usize] != keys[order[w - 1] as usize] {
                r += 1;
            }
            rank[order[w] as usize] = r;
        }
        if r as usize == n - 1 {
            break;
        }
        k *= 2;
    }
    order
}

pub fn mtf(data: &[u8]) -> Vec<u8> {
    let mut table: [u8; 256] = std::array::from_fn(|i| i as u8);
    data.iter()
        .map(|&b| {
            let j = table.iter().position(|&t| t == b).unwrap();
            table.copy_within(0..j, 1);
            table[0] = b;
            j as u8
        })
        .collect()
}

pub fn delta(data: &[u8], distance: usize) -> Vec<u8> {
    let mut out = data.to_vec();
    for i in distance..data.len() {
        out[i] = data[i].wrapping_sub(data[i - distance]);
    }
    out
}

/// Undo the BWT (`primary` in 1..=len and `rows` in 1..=len, checked by the caller),
/// running one walk per stored row at once: they are independent, so the CPU overlaps their
/// memory reads. Fails if the stored rows don't match the walk.
pub fn inverse_bwt(last: &[u8], primary: usize, rows: &[usize]) -> Option<Vec<u8>> {
    // Each table entry packs the next row with the byte found there, so every step reads
    // one random entry instead of two.
    if last.len() < 1 << 24 {
        walk::<u32>(last, primary, rows)
    } else {
        walk::<u64>(last, primary, rows)
    }
}

trait Packed: Copy + Default {
    fn pack(row: usize, byte: u8) -> Self;
    fn unpack(self) -> (usize, u8);
}

impl Packed for u32 {
    fn pack(row: usize, byte: u8) -> Self {
        ((row as u32) << 8) | u32::from(byte)
    }
    fn unpack(self) -> (usize, u8) {
        ((self >> 8) as usize, self as u8)
    }
}

impl Packed for u64 {
    fn pack(row: usize, byte: u8) -> Self {
        ((row as u64) << 8) | u64::from(byte)
    }
    fn unpack(self) -> (usize, u8) {
        ((self >> 8) as usize, self as u8)
    }
}

/// The walk over the n + 1 rows: row r starts with the byte packed in entry r and continues
/// at the row packed with it. Row 0 (the marker) continues at the primary row.
fn next_table<T: Packed>(last: &[u8], primary: usize) -> Vec<T> {
    // stable counting sort of the last column, with the marker's entry put back at `primary`
    let mut starts = [0usize; 256];
    for &b in last {
        starts[b as usize] += 1;
    }
    let mut sum = 1; // row 0 is the marker
    for s in starts.iter_mut() {
        let count = *s;
        *s = sum;
        sum += count;
    }
    let mut table = vec![T::default(); last.len() + 1];
    table[0] = T::pack(primary, 0);
    for (i, &b) in last.iter().enumerate() {
        let row = i + usize::from(i >= primary);
        table[starts[b as usize]] = T::pack(row, b);
        starts[b as usize] += 1;
    }
    table
}

fn walk<T: Packed>(last: &[u8], primary: usize, rows: &[usize]) -> Option<Vec<u8>> {
    let n = last.len();
    if n == 0 {
        return Some(Vec::new());
    }
    let table = next_table::<T>(last, primary);
    let walks = rows.len() + 1;
    let length = n.div_ceil(walks);
    let mut at: Vec<usize> = std::iter::once(primary)
        .chain(rows.iter().copied())
        .collect();
    let mut out = vec![0u8; n];
    for step in 0..length {
        for (j, row) in at.iter_mut().enumerate() {
            let pos = j * length + step;
            if pos < n {
                let (next, byte) = table[*row].unpack();
                out[pos] = byte;
                *row = next;
            }
        }
    }
    // each walk must end exactly where the next one started
    for j in 0..walks - 1 {
        if at[j] != rows[j] {
            return None;
        }
    }
    Some(out)
}

pub fn inverse_mtf(data: &[u8]) -> Vec<u8> {
    let mut table: [u8; 256] = std::array::from_fn(|i| i as u8);
    data.iter()
        .map(|&j| {
            let j = j as usize;
            let b = table[j];
            table.copy_within(0..j, 1);
            table[0] = b;
            b
        })
        .collect()
}

/// The x86 call filter (spec: "x86 calls"), in place: the 32-bit relative address after each
/// CALL (E8) or JMP (E9) whose top byte is 00 or FF becomes absolute (`forward`) or relative
/// again, so that calls to the same place look alike.
pub fn x86_calls(data: &mut [u8], forward: bool) {
    let mut i = 0;
    while i + 5 <= data.len() {
        if data[i] != 0xE8 && data[i] != 0xE9 {
            i += 1;
            continue;
        }
        if data[i + 4] == 0x00 || data[i + 4] == 0xFF {
            let v = u32::from_le_bytes(data[i + 1..i + 5].try_into().unwrap());
            let next = (i + 5) as u32; // blocks are below 4 GiB
            let w = if forward {
                v.wrapping_add(next)
            } else {
                v.wrapping_sub(next)
            };
            // bit 24 copied into bits 25-31: the top byte stays 00 or FF
            let w = (((w << 7) as i32) >> 7) as u32;
            data[i + 1..i + 5].copy_from_slice(&w.to_le_bytes());
        }
        i += 5; // the address is never itself scanned for E8 or E9
    }
}

/// How many addresses `x86_calls` converts in `data` (the inverse finds the same ones).
pub fn x86_call_count(data: &[u8]) -> usize {
    let (mut i, mut calls) = (0, 0);
    while i + 5 <= data.len() {
        if data[i] != 0xE8 && data[i] != 0xE9 {
            i += 1;
            continue;
        }
        calls += usize::from(data[i + 4] == 0x00 || data[i + 4] == 0xFF);
        i += 5;
    }
    calls
}

/// Undo `delta[i] = x[i] - x[i - distance]` (mod 256) in place.
pub fn inverse_delta(data: &mut [u8], distance: u64) {
    if distance == 0 || distance >= data.len() as u64 {
        return;
    }
    let d = distance as usize;
    for i in d..data.len() {
        data[i] = data[i].wrapping_add(data[i - d]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The BWT from the doubling reference instead of divsufsort.
    fn reference(data: &[u8]) -> (Vec<u8>, usize, Vec<usize>) {
        if data.is_empty() {
            return (Vec::new(), 0, Vec::new());
        }
        let sa = suffix_array_by_doubling(data);
        from_suffix_array(data, sa.iter().map(|&s| s as usize))
    }

    /// The walk rows found by walking (the spec's definition).
    fn walk_rows(last: &[u8], primary: usize) -> Vec<usize> {
        let n = last.len();
        let k = walk_count(n);
        let length = n.div_ceil(k);
        let table = next_table::<u64>(last, primary);
        let mut rows = Vec::new();
        let mut row = primary;
        for i in 1..=(k - 1) * length {
            row = table[row].unpack().0;
            if i % length == 0 {
                rows.push(row);
            }
        }
        rows
    }

    #[test]
    fn x86_call_filter() {
        // the E8 at 0 (relative 0) and the E9 at 5 jumping back to itself (relative -5) both go
        // to position 5, and both become 5; an address whose top byte isn't 00 or FF is kept
        let data = [
            0xE8, 0, 0, 0, 0, 0xE9, 0xFB, 0xFF, 0xFF, 0xFF, 0xE8, 0x12, 0x34, 0x56, 0x78,
        ];
        let mut out = data;
        x86_calls(&mut out, true);
        assert_eq!(out[..10], [0xE8, 5, 0, 0, 0, 0xE9, 5, 0, 0, 0]);
        assert_eq!(out[10..], data[10..]);
        assert_eq!(x86_call_count(&data), 2);
        x86_calls(&mut out, false);
        assert_eq!(out, data);
        // the address after an E8 is never scanned itself; nor is an E8 without 4 bytes after it
        assert_eq!(x86_call_count(&[0xE8, 0xE8, 0, 0, 0, 0]), 1);
        assert_eq!(x86_call_count(&[0x90, 0xE8, 0, 0, 0]), 0);
        // a sum that reaches bit 24 still has a top byte of 00 or FF: bit 24 is copied upwards
        let mut wide = [0x90, 0x90, 0x90, 0xE8, 0xFF, 0xFF, 0xFF, 0x00];
        x86_calls(&mut wide, true);
        assert_eq!(wide[4..], [0x07, 0x00, 0x00, 0xFF]);
        // bytes rich in E8, E9, 00 and FF: the inverse finds the same calls and undoes them
        let mut seed = 99u64;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for _ in 0..500 {
            let len = (next() % 60) as usize;
            let data: Vec<u8> = (0..len)
                .map(|_| [0xE8, 0xE9, 0x00, 0xFF, next() as u8][(next() % 5) as usize])
                .collect();
            let mut out = data.clone();
            x86_calls(&mut out, true);
            assert_eq!(x86_call_count(&out), x86_call_count(&data));
            x86_calls(&mut out, false);
            assert_eq!(out, data);
        }
    }

    #[test]
    fn banana() {
        // sorted suffixes of "banana$": $, a$, ana$, anana$, banana$, na$, nana$
        assert_eq!(bwt(b"banana"), (b"annbaa".to_vec(), 4));
        assert_eq!(inverse_bwt(b"annbaa", 4, &[]).unwrap(), b"banana");
        assert_eq!(bwt(b"aaa"), (b"aaa".to_vec(), 3));
    }

    #[test]
    fn fast_bwt_matches_the_reference() {
        let mut seed = 12345u64;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let mut inputs: Vec<Vec<u8>> = vec![
            b"a".to_vec(),
            b"banana".to_vec(),
            b"abab".to_vec(),
            vec![7; 100],
            vec![0; 3],
            b"abcabcabcabc".to_vec(),
        ];
        for _ in 0..300 {
            let len = (next() % 400) as usize + 1;
            let alphabet = (next() % 4) as u8 + 1;
            let chunk: Vec<u8> = (0..(next() % 9 + 1))
                .map(|_| (next() % u64::from(alphabet)) as u8)
                .collect();
            let data: Vec<u8> = if next() % 2 == 0 {
                (0..len).map(|i| chunk[i % chunk.len()]).collect() // often periodic
            } else {
                (0..len)
                    .map(|_| (next() % u64::from(alphabet)) as u8)
                    .collect()
            };
            inputs.push(data);
        }
        for data in inputs {
            assert_eq!(bwt_with_rows(&data), reference(&data), "{data:?}");
            let (last, primary) = bwt(&data);
            assert_eq!(inverse_bwt(&last, primary, &[]).unwrap(), data);
        }
    }

    #[test]
    fn stored_rows_match_the_walk_and_restore_the_data() {
        let text: Vec<u8> = (0..300_000u32)
            .map(|i| b"paw series map gap "[(i * 7 % 19) as usize])
            .collect();
        let periodic: Vec<u8> = b"abcab".iter().copied().cycle().take(5 * 60_000).collect();
        for data in [text, periodic, vec![0; 400_000]] {
            let (last, primary, rows) = bwt_with_rows(&data);
            assert_eq!(rows.len(), walk_count(data.len()) - 1);
            assert!(!rows.is_empty());
            assert_eq!(rows, walk_rows(&last, primary));
            assert_eq!(inverse_bwt(&last, primary, &rows).unwrap(), data);
            let mut wrong = rows.clone();
            wrong[0] = wrong[0] % data.len() + 1;
            assert!(inverse_bwt(&last, primary, &wrong).is_none());
        }
    }
}
