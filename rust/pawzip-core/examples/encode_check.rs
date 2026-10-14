//! Compress `<name>.orig` files (automatic modes, 1 MiB blocks) and compare with the
//! `<name>.paw` written by the Python encoder: `encode_check DIR`.

use std::time::Instant;

fn main() {
    let dir = std::env::args().nth(1).expect("usage: encode_check DIR");
    let mut entries: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    entries.sort();
    let options = pawzip_core::Options {
        block_size: 1 << 20,
        ..Default::default()
    };
    let (mut same, mut total, mut bytes, mut secs) = (0, 0, 0usize, 0f64);
    for path in entries
        .into_iter()
        .filter(|p| p.extension().is_some_and(|e| e == "orig"))
    {
        let data = std::fs::read(&path).unwrap();
        let start = Instant::now();
        let blob = pawzip_core::compress(&data, &options).unwrap();
        let s = start.elapsed().as_secs_f64();
        let python = std::fs::read(path.with_extension("paw")).unwrap();
        let identical = blob == python;
        assert_eq!(
            pawzip_core::decompress(&blob, Default::default()).unwrap(),
            data
        );
        println!(
            "{:22} {:>9} bytes -> rust {:>8}, python {:>8}  {}  {:6.2} MB/s",
            path.file_stem().unwrap().to_string_lossy(),
            data.len(),
            blob.len(),
            python.len(),
            if identical { "identical" } else { "DIFFERENT" },
            data.len() as f64 / s / 1e6
        );
        same += usize::from(identical);
        total += 1;
        bytes += data.len();
        secs += s;
    }
    println!(
        "{same}/{total} identical to Python; {:.2} MB/s overall",
        bytes as f64 / secs / 1e6
    );
}
