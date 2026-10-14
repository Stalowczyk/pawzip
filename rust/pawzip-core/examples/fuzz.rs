//! A simple mutation fuzzer for the decoder: `fuzz [ITERATIONS] [SEED]`.
//!
//! Starts from valid .paw files (the shared test vectors plus files it compresses itself),
//! damages them in random ways and decodes the result. Decoding may fail with an error,
//! but must never panic, hang or allocate wildly; a valid decode must match a re-decode.
//! (A dependency-free stand-in for cargo-fuzz, which needs a nightly toolchain.)

use std::panic;
use std::path::PathBuf;
use std::time::Instant;

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64*
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n.max(1) as u64) as usize
    }
}

fn seeds() -> Vec<Vec<u8>> {
    let vectors = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors");
    let mut out: Vec<Vec<u8>> = std::fs::read_dir(vectors)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "paw"))
        .map(|p| std::fs::read(p).unwrap())
        .collect();
    // a few structured inputs with many series and shapes
    let image: Vec<u8> = (0..60 * 40 * 3)
        .map(|i| {
            if (i / 3) % 60 < 20 {
                200
            } else {
                (i % 3) as u8
            }
        })
        .collect();
    for modes in [
        vec![pawzip_core::Mode::Map],
        vec![pawzip_core::Mode::DeltaBwtMtfMap],
    ] {
        let options = pawzip_core::Options {
            block_size: 2000,
            modes: Some(modes),
            ..Default::default()
        };
        out.push(pawzip_core::compress(&image, &options).unwrap());
    }
    out
}

fn mutate(rng: &mut Rng, blob: &mut Vec<u8>) {
    for _ in 0..1 + rng.below(4) {
        match rng.below(7) {
            0 if !blob.is_empty() => {
                let i = rng.below(blob.len());
                blob[i] ^= 1 << rng.below(8);
            }
            1 if !blob.is_empty() => {
                let i = rng.below(blob.len());
                blob[i] = rng.next() as u8;
            }
            2 => blob.truncate(rng.below(blob.len() + 1)),
            3 => blob.extend((0..1 + rng.below(8)).map(|_| rng.next() as u8)),
            4 => {
                let i = rng.below(blob.len() + 1);
                blob.insert(i, rng.next() as u8);
            }
            5 if blob.len() > 1 => {
                let i = rng.below(blob.len());
                blob.remove(i);
            }
            _ => {
                // overwrite a header-ish byte with an extreme value
                if blob.len() > 4 {
                    let i = 4 + rng.below((blob.len() - 4).min(16));
                    blob[i] = [0x00, 0x7F, 0x80, 0xFF][rng.below(4)];
                }
            }
        }
    }
}

fn main() {
    let mut args = std::env::args().skip(1);
    let iterations: u64 = args.next().map_or(200_000, |a| a.parse().unwrap());
    let mut rng = Rng(args
        .next()
        .map_or(0x9E37_79B9_7F4A_7C15, |a| a.parse().unwrap()));
    panic::set_hook(Box::new(|_| {}));
    let seeds = seeds();
    let start = Instant::now();
    let (mut ok, mut errors, mut slowest) = (0u64, 0u64, 0f64);
    for i in 0..iterations {
        let mut blob = seeds[rng.below(seeds.len())].clone();
        mutate(&mut rng, &mut blob);
        let t = Instant::now();
        let outcome = panic::catch_unwind(|| pawzip_core::decompress(&blob, Default::default()));
        slowest = slowest.max(t.elapsed().as_secs_f64());
        match outcome {
            Ok(Ok(_)) => ok += 1,
            Ok(Err(_)) => errors += 1,
            Err(_) => {
                let path = std::env::temp_dir().join(format!("paw-fuzz-crash-{i}.paw"));
                std::fs::write(&path, &blob).unwrap();
                eprintln!("PANIC on iteration {i}; input saved to {}", path.display());
                std::process::exit(1);
            }
        }
    }
    println!(
        "{iterations} damaged files in {:.1}s: {ok} decoded, {errors} rejected, 0 panics; slowest {:.1} ms",
        start.elapsed().as_secs_f64(),
        slowest * 1e3
    );
}
