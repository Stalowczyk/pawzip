//! Compress and decompress whole files: `speed [-w WORKERS] [-b BLOCK_SIZE] [-t] FILE...`
//! (`-t`: thorough, every mode tried).
//! Workers are used for both directions (default 1); the block size defaults to 16 MiB.

use std::time::Instant;

fn main() {
    let mut options = pawzip_core::Options::default();
    let mut decode = pawzip_core::DecodeOptions::default();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-w" => {
                options.workers = args.next().unwrap().parse().unwrap();
                decode.workers = options.workers;
            }
            "-t" => options.thorough = true,
            "-b" => options.block_size = args.next().unwrap().parse().unwrap(),
            path => {
                let data = std::fs::read(path).unwrap();
                let start = Instant::now();
                let blob = pawzip_core::compress(&data, &options).unwrap();
                let c = start.elapsed().as_secs_f64();
                let start = Instant::now();
                let back = pawzip_core::decompress(&blob, decode).unwrap();
                let d = start.elapsed().as_secs_f64();
                assert!(back == data);
                println!(
                    "{path}: {} B {:.1}% | compress {:.1}s ({:.2} MB/s) | decompress {:.2}s ({:.1} MB/s)",
                    blob.len(),
                    100.0 * blob.len() as f64 / data.len() as f64,
                    c,
                    data.len() as f64 / c / 1e6,
                    d,
                    data.len() as f64 / d / 1e6
                );
            }
        }
    }
}
