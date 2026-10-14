//! Decode .paw files and compare with the originals: `decode_check DIR` expects
//! `<name>.paw` next to `<name>.orig`, and prints sizes and decoding speed.

use std::time::Instant;

fn main() {
    let dir = std::env::args().nth(1).expect("usage: decode_check DIR");
    let (mut total_in, mut total_out, mut total_s) = (0usize, 0usize, 0f64);
    let mut entries: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    entries.sort();
    for path in entries
        .into_iter()
        .filter(|p| p.extension().is_some_and(|e| e == "paw"))
    {
        let blob = std::fs::read(&path).unwrap();
        let original = std::fs::read(path.with_extension("orig")).unwrap();
        let start = Instant::now();
        let data = pawzip_core::decompress(&blob, Default::default()).unwrap();
        let s = start.elapsed().as_secs_f64();
        assert!(data == original, "{}: mismatch", path.display());
        println!(
            "{:28} {:>10} -> {:>10} bytes  {:7.1} MB/s",
            path.file_stem().unwrap().to_string_lossy(),
            blob.len(),
            data.len(),
            data.len() as f64 / s / 1e6
        );
        total_in += blob.len();
        total_out += data.len();
        total_s += s;
    }
    println!(
        "total: {total_in} -> {total_out} bytes, {:.1} MB/s, all identical",
        total_out as f64 / total_s / 1e6
    );
}
