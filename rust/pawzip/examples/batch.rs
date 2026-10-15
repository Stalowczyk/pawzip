//! Batch tool for differential testing against the Python implementation.
//!
//! `batch DIR` processes, for every case in DIR:
//! - `<id>.in` + `<id>.opt` ("modes block_size thorough extension", modes like `1,2` or `auto`,
//!   extension or `-`): writes `<id>.rs.paw`;
//! - `<id>.mut`: decodes it and writes `<id>.rs.out`: `ok <len> <crc32 hex>` or `err <kind>`.
//!
//! A panic is caught and reported as `err panic`, so a bug never hides behind a crash.

use std::panic;
use std::path::Path;

use pawzip::{CompressOptions, Error, Mode};

fn options(spec: &str) -> CompressOptions {
    let parts: Vec<&str> = spec.split_whitespace().collect();
    let modes = (parts[0] != "auto").then(|| {
        parts[0]
            .split(',')
            .map(|m| Mode::from_byte(m.parse().unwrap()).unwrap())
            .collect()
    });
    let mut options = CompressOptions::default();
    options.block_size = parts[1].parse().unwrap();
    options.modes = modes;
    options.thorough = parts[2] == "1";
    options.extension = (parts[3] != "-").then(|| parts[3].to_string());
    options.workers = 2;
    options
}

fn decode_outcome(blob: &[u8]) -> String {
    match panic::catch_unwind(|| pawzip::decompress(blob, &Default::default())) {
        Ok(Ok(data)) => format!("ok {} {:08x}", data.len(), pawzip::internal::crc32(&data)),
        Ok(Err(Error::Format(_))) => "err format".into(),
        Ok(Err(Error::SizeLimit(_))) => "err sizelimit".into(),
        Ok(Err(e)) => format!("err other {e}"), // I/O or options: not expected in memory
        Err(_) => "err panic".into(),
    }
}

fn main() {
    let dir = std::env::args().nth(1).expect("usage: batch DIR");
    panic::set_hook(Box::new(|_| {})); // panics are reported in the outcome files
    let (mut encoded, mut decoded) = (0, 0);
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        match path.extension().and_then(|e| e.to_str()) {
            Some("in") => {
                let data = std::fs::read(&path).unwrap();
                let spec = std::fs::read_to_string(path.with_extension("opt")).unwrap();
                let opts = options(&spec);
                let blob = match panic::catch_unwind(|| pawzip::compress(&data, &opts)) {
                    Ok(Ok(blob)) => blob,
                    Ok(Err(e)) => format!("ERROR {e}").into_bytes(),
                    Err(_) => b"PANIC".to_vec(),
                };
                std::fs::write(with_suffix(&path, "rs.paw"), blob).unwrap();
                encoded += 1;
            }
            Some("mut") => {
                let blob = std::fs::read(&path).unwrap();
                std::fs::write(with_suffix(&path, "rs.out"), decode_outcome(&blob)).unwrap();
                decoded += 1;
            }
            _ => {}
        }
    }
    println!("rust: {encoded} encoded, {decoded} decoded");
}

fn with_suffix(path: &Path, suffix: &str) -> std::path::PathBuf {
    path.with_extension(suffix)
}
