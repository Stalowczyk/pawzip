//! `max_block_size` bounds the memory a block takes: its data, and its body, which is refused
//! from its header, before it's read, when it's far larger than any block needs.

use pawzip::{DecompressOptions, Error};

fn varint(mut n: u64) -> Vec<u8> {
    let mut out = Vec::new();
    while n >= 0x80 {
        out.push((n as u8 & 0x7F) | 0x80);
        n >>= 7;
    }
    out.push(n as u8);
    out
}

/// The start of a .paw stream whose one 1-byte block declares a body of `body_size` bytes, and
/// ends there. (Bytes a decoder never reads may pad a body, so such a block can be valid.)
fn declared_body(body_size: u64) -> Vec<u8> {
    let mut paw = b"PAW\x01".to_vec();
    paw.extend(varint(1)); // block size
    paw.extend([0, 0]); // no extension, no extra data
    paw.extend(varint(1)); // one byte
    paw.push(1); // map
    paw.extend([0; 4]); // CRC-32
    paw.extend(varint(body_size));
    paw
}

fn limited(max_block_size: Option<u64>) -> DecompressOptions {
    let mut options = DecompressOptions::default();
    options.max_block_size = max_block_size;
    options
}

#[test]
fn a_body_far_larger_than_max_block_size_is_refused_from_its_header() {
    let largest = 2 * 1000 + 1024;
    match pawzip::decompress(&declared_body(largest + 1), &limited(Some(1000))) {
        Err(Error::SizeLimit(message)) => assert_eq!(
            message,
            "a block's body is 3,025 bytes, more than max_block_size=1,000 allows"
        ),
        other => panic!("expected a size limit error, got {other:?}"),
    }
    // Allowed, then missing.
    match pawzip::decompress(&declared_body(largest), &limited(Some(1000))) {
        Err(Error::Format(message)) => assert_eq!(message, "unexpected end of data"),
        other => panic!("expected a format error, got {other:?}"),
    }
    // Without a limit, any body is waited for.
    let error = pawzip::decompress(&declared_body(1 << 40), &limited(None)).unwrap_err();
    assert!(matches!(error, Error::Format(_)), "{error:?}");
}

#[test]
fn bodies_larger_than_their_data_are_allowed() {
    // Random data doesn't compress: forced into a mode other than stored, its body is a little
    // larger than the data. A limit of the block size still lets it through.
    let mut state = 1u64;
    let data: Vec<u8> = (0..300_000)
        .map(|_| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 56) as u8
        })
        .collect();
    let mut options = pawzip::CompressOptions::default();
    options.block_size = 100_000;
    for mode in [
        pawzip::Mode::Map,
        pawzip::Mode::BwtMtfMap,
        pawzip::Mode::DeltaBwtMap,
    ] {
        options.modes = Some(vec![mode]);
        let paw = pawzip::compress(&data, &options).unwrap();
        assert!(paw.len() > data.len(), "{mode}: larger than the data");
        let back = pawzip::decompress(&paw, &limited(Some(100_000))).unwrap();
        assert!(back == data, "{mode}");
    }
}
