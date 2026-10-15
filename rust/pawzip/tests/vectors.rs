//! Every shared test vector must decode exactly to its input (spec/vectors/).

use std::fs;
use std::path::PathBuf;

fn vectors() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors")
}

#[test]
fn all_vectors_decode_exactly() {
    let mut checked = 0;
    for entry in fs::read_dir(vectors()).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|e| e.to_str()) != Some("paw") {
            continue;
        }
        // "<input>.mode<k>[.blocks<n>].paw" was made from "<input>.bin"
        let name = path.file_name().unwrap().to_str().unwrap();
        let input = name.split(".mode").next().unwrap();
        let expected = fs::read(vectors().join(format!("{input}.bin"))).unwrap();
        let blob = fs::read(&path).unwrap();
        let data = pawzip::decompress(&blob, &Default::default())
            .unwrap_or_else(|e| panic!("{name}: {e}"));
        assert!(data == expected, "{name}: decoded data differs");
        let info = pawzip::info(&blob).unwrap();
        assert_eq!(info.original_size, expected.len() as u64, "{name}");
        checked += 1;
    }
    assert_eq!(checked, 150);
}

/// The Rust encoder writes exactly the bytes the Python encoder wrote for every vector.
#[test]
fn encoder_reproduces_every_vector() {
    let mut checked = 0;
    for entry in fs::read_dir(vectors()).unwrap() {
        let path = entry.unwrap().path();
        let name = path.file_name().unwrap().to_str().unwrap().to_string();
        let Some(rest) = name.strip_suffix(".paw") else {
            continue;
        };
        // "<input>.mode<k>[.blocks<n>]"
        let (input, spec) = rest.split_once(".mode").unwrap();
        let (mode, block_size) = match spec.split_once(".blocks") {
            Some((m, b)) => (m, b.parse().unwrap()),
            None => (spec, 16 * 1024 * 1024),
        };
        let mode = pawzip::Mode::from_byte(mode.parse().unwrap()).unwrap();
        let data = fs::read(vectors().join(format!("{input}.bin"))).unwrap();
        let mut options = pawzip::CompressOptions::default();
        options.block_size = block_size;
        options.modes = Some(vec![mode]);
        let blob = pawzip::compress(&data, &options).unwrap();
        assert!(
            blob == fs::read(&path).unwrap(),
            "{name}: encoder output differs from Python's"
        );
        checked += 1;
    }
    assert_eq!(checked, 150);
}

/// Valid files the reference encoder never writes: "<input>.<case>.paw" decodes to
/// "<input>.bin" (spec/vectors/unusual/).
#[test]
fn unusual_vectors_decode_exactly() {
    let dir = vectors().join("unusual");
    let mut checked = 0;
    for entry in fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|e| e.to_str()) != Some("paw") {
            continue;
        }
        let name = path.file_name().unwrap().to_str().unwrap();
        let input = name.split('.').next().unwrap();
        let expected = fs::read(dir.join(format!("{input}.bin"))).unwrap();
        let data = pawzip::decompress(&fs::read(&path).unwrap(), &Default::default())
            .unwrap_or_else(|e| panic!("{name}: {e}"));
        assert!(data == expected, "{name}: decoded data differs");
        checked += 1;
    }
    assert_eq!(checked, 9);
}

/// One file per rule of the spec's "Validation" section: each must be rejected
/// (spec/vectors/invalid/).
#[test]
fn invalid_vectors_are_rejected() {
    let mut checked = 0;
    for entry in fs::read_dir(vectors().join("invalid")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|e| e.to_str()) != Some("paw") {
            continue;
        }
        let blob = fs::read(&path).unwrap();
        assert!(
            pawzip::decompress(&blob, &Default::default()).is_err(),
            "{} was accepted",
            path.display()
        );
        checked += 1;
    }
    assert_eq!(checked, 42);
}

/// Damaged files the fuzzer once crashed on: each must be rejected cleanly.
#[test]
fn fuzzer_regressions_are_rejected() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/regressions");
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        let blob = fs::read(&path).unwrap();
        assert!(
            pawzip::decompress(&blob, &Default::default()).is_err(),
            "{}",
            path.display()
        );
    }
}

/// Damage around the BWT header (primary index and walk rows) of the large vectors: every
/// result must be a clean error or the original data.
#[test]
fn damaged_walk_rows_are_rejected() {
    for mode in [2, 4] {
        let blob = fs::read(vectors().join(format!("long-text.mode{mode}.paw"))).unwrap();
        let expected = fs::read(vectors().join("long-text.bin")).unwrap();
        for i in 4..40 {
            for flip in [0x01, 0x40, 0xff] {
                let mut damaged = blob.clone();
                damaged[i] ^= flip;
                if let Ok(data) = pawzip::decompress(&damaged, &Default::default()) {
                    assert!(data == expected, "mode {mode}, byte {i}");
                }
            }
        }
    }
}
