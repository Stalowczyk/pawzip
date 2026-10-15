//! Decoding with several workers gives exactly what one worker gives: the same data, or the
//! same error for a damaged file.

use pawzip::{compress, decompress, CompressOptions, DecompressOptions};

fn decode(blob: &[u8], workers: usize) -> Result<Vec<u8>, String> {
    let mut options = DecompressOptions::default();
    options.workers = workers;
    decompress(blob, &options).map_err(|e| e.to_string())
}

#[test]
fn workers_agree_with_one_worker() {
    let data: Vec<u8> = (0..40_000u32)
        .map(|i| b"the walk, the map and the series "[(i * 5 % 33) as usize] ^ (i / 9000) as u8)
        .collect();
    let mut options = CompressOptions::default();
    options.block_size = 3000;
    let blob = compress(&data, &options).unwrap();
    for workers in [2, 3, 8] {
        assert!(decode(&blob, workers).unwrap() == data);
    }
    let mut seed = 99u64;
    for _ in 0..600 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let mut damaged = blob.clone();
        let at = (seed % blob.len() as u64) as usize;
        damaged[at] ^= (seed >> 32) as u8 | 1;
        if seed.is_multiple_of(5) {
            damaged.truncate(at); // also cut it short sometimes
        }
        let one = decode(&damaged, 1);
        for workers in [2, 3, 8] {
            assert_eq!(
                decode(&damaged, workers),
                one,
                "byte {at}, {workers} workers"
            );
        }
    }
}
