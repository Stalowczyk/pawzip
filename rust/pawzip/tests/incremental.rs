//! Compressing and decompressing input that arrives in pieces (`internal::Compressor` and
//! `internal::Decompressor`, used by the npm package) gives exactly the results of
//! `compress_stream` and `decompress_stream`, wherever the input is cut: the same bytes, and
//! the same errors after the same output.

use pawzip::internal::{Compressor, Decompressor};
use pawzip::{CompressOptions, DecompressOptions, Mode};
use std::fs;
use std::path::{Path, PathBuf};

/// Piece sizes to cut the input into; `usize::MAX` gives all of it at once.
const CUTS: [usize; 9] = [1, 2, 3, 5, 8, 13, 64, 1000, usize::MAX];

fn vectors() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors")
}

fn paw_files(dir: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().and_then(|e| e.to_str()) == Some("paw"))
        .collect();
    files.sort();
    files
}

/// Every .paw file the tests have: valid, unusual, invalid, and the fuzzer's regressions.
fn all_paw_files() -> Vec<PathBuf> {
    let mut files = paw_files(&vectors());
    files.extend(paw_files(&vectors().join("unusual")));
    files.extend(paw_files(&vectors().join("invalid")));
    files.extend(paw_files(
        &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/regressions"),
    ));
    assert_eq!(files.len(), 150 + 9 + 42 + 1);
    files
}

/// What decompressing gives: the data written, then the error that stopped it, if one did (as
/// text, so that errors compare by kind and message).
type Outcome = (Vec<u8>, Option<String>);

fn decompress_stream(blob: &[u8], options: DecompressOptions) -> Outcome {
    let mut out = Vec::new();
    let error = pawzip::decompress_stream(blob, &mut out, &options, &mut |_| {}).err();
    (out, error.map(|e| format!("{e:?}")))
}

/// The same through `Decompressor`, given `blob` in pieces of `cut` bytes and taking each
/// block as soon as it's complete. A finished stream must have used all of its input, and given
/// one block per call: as many as the file has.
fn decompress_in_pieces(blob: &[u8], cut: usize, options: DecompressOptions) -> Outcome {
    let mut decompressor = Decompressor::new(options);
    let mut out = Vec::new();
    let mut blocks = 0;
    let fed = feed(&mut decompressor, blob, cut, &mut out, &mut blocks);
    let consumed = decompressor.consumed();
    let error = fed.and_then(|()| decompressor.finish()).err();
    if error.is_none() {
        assert_eq!(
            consumed,
            blob.len() as u64,
            "a finished stream used all of its input"
        );
        let expected = pawzip::info(blob).unwrap().modes.len();
        assert_eq!(blocks, expected, "one block per call");
    }
    (out, error.map(|e| format!("{e:?}")))
}

fn feed(
    decompressor: &mut Decompressor,
    blob: &[u8],
    cut: usize,
    out: &mut Vec<u8>,
    blocks: &mut usize,
) -> pawzip::Result<()> {
    for piece in blob.chunks(cut) {
        decompressor.push(piece)?;
        while let Some(block) = decompressor.next_block()? {
            out.extend(block);
            *blocks += 1;
        }
    }
    Ok(())
}

fn assert_same_as_decompress_stream(blob: &[u8], name: &str, options: DecompressOptions) {
    let (data, error) = decompress_stream(blob, options);
    for cut in CUTS {
        let (got, got_error) = decompress_in_pieces(blob, cut, options);
        assert!(
            got == data && got_error == error,
            "{name}, pieces of {cut}: {} bytes, then {got_error:?}, instead of {} bytes, then {error:?}",
            got.len(),
            data.len(),
        );
    }
}

fn compress_in_pieces(data: &[u8], cut: usize, options: CompressOptions) -> Vec<u8> {
    let block_size = options.block_size as usize;
    let mut compressor = Compressor::new(options).unwrap();
    let mut out = Vec::new();
    for piece in data.chunks(cut) {
        out.extend(compressor.push(piece));
    }
    assert_eq!(
        compressor.done() as usize,
        data.len() - data.len() % block_size,
        "done counts the input in finished blocks"
    );
    out.extend(compressor.finish());
    out
}

#[test]
fn every_file_decodes_as_with_decompress_stream() {
    for path in all_paw_files() {
        let blob = fs::read(&path).unwrap();
        assert_same_as_decompress_stream(&blob, &path.display().to_string(), Default::default());
    }
}

#[test]
fn size_limits_stop_at_the_same_block() {
    for path in paw_files(&vectors()) {
        let blob = fs::read(&path).unwrap();
        let size = pawzip::info(&blob).unwrap().original_size;
        let name = path.display().to_string();
        for (max_size, max_block_size) in [(size.saturating_sub(1), None), (size, Some(499))] {
            let mut options = DecompressOptions::default();
            options.max_size = Some(max_size);
            options.max_block_size = max_block_size;
            assert_same_as_decompress_stream(&blob, &name, options);
        }
    }
}

/// Cut short anywhere, or with a byte changed near the start, a file gives the same data and
/// the same error both ways.
#[test]
fn damaged_files_fail_as_with_decompress_stream() {
    for name in [
        "text.mode5.paw",
        "x86-code.mode7.blocks500.paw",
        "small-image.mode4.paw",
    ] {
        let blob = fs::read(vectors().join(name)).unwrap();
        for length in 0..blob.len() {
            assert_same_as_decompress_stream(
                &blob[..length],
                &format!("{name}[..{length}]"),
                Default::default(),
            );
        }
        for i in 0..blob.len().min(48) {
            for flip in [0x01, 0x80, 0xff] {
                let mut damaged = blob.clone();
                damaged[i] ^= flip;
                assert_same_as_decompress_stream(
                    &damaged,
                    &format!("{name}, byte {i} ^ {flip:#x}"),
                    Default::default(),
                );
            }
        }
        let mut trailing = blob.clone();
        trailing.push(0);
        let name = format!("{name} + 1 byte");
        assert_same_as_decompress_stream(&trailing, &name, Default::default());
    }
}

/// A damaged block late in the file: the blocks before it come out first, as with
/// `decompress_stream`, however the input is cut.
#[test]
fn blocks_before_an_error_are_given() {
    let mut options = CompressOptions::default();
    options.block_size = 1000;
    let data = b"some text, then more text\n".repeat(400); // 10,400 bytes: 11 blocks
    let mut blob = pawzip::compress(&data, &options).unwrap();
    let last = blob.len() - 5; // inside the last block's body
    blob[last] ^= 0xff;
    let (out, error) = decompress_stream(&blob, Default::default());
    assert_eq!(out, data[..10_000], "ten good blocks");
    assert!(error.is_some());
    assert_same_as_decompress_stream(&blob, "last block damaged", Default::default());
}

/// However much the data expands, each call gives one block, and only when it's asked for.
#[test]
fn blocks_are_decoded_one_at_a_time() {
    let mut options = CompressOptions::default();
    options.block_size = 1 << 16;
    options.modes = Some(vec![Mode::Map]);
    let blob = pawzip::compress(&vec![0; 64 << 16], &options).unwrap(); // 64 blocks of zeros
    assert!(blob.len() < 4096, "a few bytes per block");
    let mut decompressor = Decompressor::new(Default::default());
    decompressor.push(&blob).unwrap();
    let mut blocks = 0;
    let mut consumed = 0;
    while let Some(block) = decompressor.next_block().unwrap() {
        assert_eq!(block.len(), 1 << 16, "one block");
        assert!(
            decompressor.consumed() > consumed,
            "only that block's input used"
        );
        consumed = decompressor.consumed();
        blocks += 1;
    }
    assert_eq!(blocks, 64);
    decompressor.finish().unwrap();
}

/// Data after the end record is refused, in the same piece or in a later one.
#[test]
fn data_after_the_end_is_refused() {
    let blob = pawzip::compress(b"abc", &Default::default()).unwrap();
    let mut decompressor = Decompressor::new(Default::default());
    decompressor.push(&blob).unwrap();
    assert_eq!(decompressor.next_block().unwrap().unwrap(), b"abc");
    assert_eq!(decompressor.next_block().unwrap(), None);
    decompressor.push(&[]).unwrap();
    let error = decompressor.push(&[0]).unwrap_err();
    assert_eq!(
        error.to_string(),
        "unexpected data after the end of the .paw stream"
    );
}

/// The encoder writes exactly the bytes of every vector, wherever its input is cut.
#[test]
fn compressor_reproduces_every_vector() {
    let mut checked = 0;
    for path in paw_files(&vectors()) {
        let name = path.file_name().unwrap().to_str().unwrap();
        // "<input>.mode<k>[.blocks<n>].paw"
        let (input, spec) = name
            .strip_suffix(".paw")
            .unwrap()
            .split_once(".mode")
            .unwrap();
        let (mode, block_size) = match spec.split_once(".blocks") {
            Some((m, b)) => (m, b.parse().unwrap()),
            None => (spec, pawzip::DEFAULT_BLOCK_SIZE),
        };
        let data = fs::read(vectors().join(format!("{input}.bin"))).unwrap();
        let expected = fs::read(&path).unwrap();
        let mut options = CompressOptions::default();
        options.block_size = block_size;
        options.modes = Some(vec![Mode::from_byte(mode.parse().unwrap()).unwrap()]);
        for cut in [1, 7, 500, 4096, usize::MAX] {
            let got = compress_in_pieces(&data, cut, options.clone());
            assert!(
                got == expected,
                "{name}, pieces of {cut}: differs from the vector"
            );
        }
        checked += 1;
    }
    assert_eq!(checked, 150);
}

/// With automatic modes, an extension, and blocks that end exactly where the input does.
#[test]
fn compressor_matches_compress() {
    let text = b"the quick brown fox jumps over the lazy dog\n".repeat(300); // 13,200 bytes
    let cases: [(u64, &[u8]); 8] = [
        (1, &text[..40]),
        (99, &text[..2000]),
        (4400, &text),             // three blocks, the last one full
        (13_200, &text),           // one full block
        (13_200, &text[..13_199]), // one byte short of a block
        (1 << 20, &text),
        (1 << 20, &[]),
        (1 << 20, &[7]),
    ];
    for (block_size, data) in cases {
        let mut options = CompressOptions::default();
        options.extension = Some(".txt".into());
        options.block_size = block_size;
        let expected = pawzip::compress(data, &options).unwrap();
        for cut in [1, 3, 4400, usize::MAX] {
            let got = compress_in_pieces(data, cut, options.clone());
            assert!(
                got == expected,
                "{} bytes, blocks of {block_size}, pieces of {cut}",
                data.len()
            );
        }
    }
}

#[test]
fn compressor_checks_its_options() {
    let mut options = CompressOptions::default();
    options.extension = Some("no spaces".into());
    assert!(matches!(
        Compressor::new(options),
        Err(pawzip::Error::Invalid(_))
    ));
    let mut options = CompressOptions::default();
    options.block_size = 0;
    assert!(matches!(
        Compressor::new(options),
        Err(pawzip::Error::Invalid(_))
    ));
}
