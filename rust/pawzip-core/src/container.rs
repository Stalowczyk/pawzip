//! The .paw container: header, independent blocks, end record (spec/FORMAT.md).

use crate::crc32::crc32;
use crate::error::{format, group_thousands, Error, Result};
use crate::mapcodec::decode_map;
use crate::transforms::{inverse_bwt, inverse_delta, inverse_mtf, walk_count, x86_calls};
use crate::varint::{Reader, Source, StreamSource};
use std::io::{Read, Write};

pub const MAGIC: &[u8; 3] = b"PAW";
pub const FORMAT_VERSION: u8 = 1;
pub const DEFAULT_BLOCK_SIZE: u64 = 16 * 1024 * 1024;
pub const DEFAULT_MAX_BLOCK_SIZE: u64 = 64 * 1024 * 1024;
pub const FORMAT_MAX_BLOCK_SIZE: u64 = (1 << 32) - 1;
pub const MAX_EXTENSION: usize = 16;
/// Optional header data that decoders skip (written empty by v1 encoders; see the spec).
pub const MAX_HEADER_EXTRA: u64 = 1 << 16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Stored = 0,
    Map = 1,
    BwtMtfMap = 2,
    DeltaMap = 3,
    DeltaBwtMtfMap = 4,
    BwtMap = 5,
    DeltaBwtMap = 6,
    X86BwtMap = 7,
}

impl Mode {
    pub fn from_byte(b: u8) -> Option<Mode> {
        Some(match b {
            0 => Mode::Stored,
            1 => Mode::Map,
            2 => Mode::BwtMtfMap,
            3 => Mode::DeltaMap,
            4 => Mode::DeltaBwtMtfMap,
            5 => Mode::BwtMap,
            6 => Mode::DeltaBwtMap,
            7 => Mode::X86BwtMap,
            _ => return None,
        })
    }

    pub fn label(self) -> &'static str {
        match self {
            Mode::Stored => "stored",
            Mode::Map => "map",
            Mode::BwtMtfMap => "bwt-mtf-map",
            Mode::DeltaMap => "delta-map",
            Mode::DeltaBwtMtfMap => "delta-bwt-mtf-map",
            Mode::BwtMap => "bwt-map",
            Mode::DeltaBwtMap => "delta-bwt-map",
            Mode::X86BwtMap => "x86-bwt-map",
        }
    }

    pub(crate) fn uses_delta(self) -> bool {
        matches!(
            self,
            Mode::DeltaMap | Mode::DeltaBwtMtfMap | Mode::DeltaBwtMap
        )
    }

    pub(crate) fn uses_x86(self) -> bool {
        self == Mode::X86BwtMap
    }

    pub(crate) fn uses_bwt(self) -> bool {
        matches!(
            self,
            Mode::BwtMtfMap
                | Mode::DeltaBwtMtfMap
                | Mode::BwtMap
                | Mode::DeltaBwtMap
                | Mode::X86BwtMap
        )
    }

    pub(crate) fn uses_mtf(self) -> bool {
        matches!(self, Mode::BwtMtfMap | Mode::DeltaBwtMtfMap)
    }
}

pub fn extension_allowed(ext: &[u8]) -> bool {
    ext.len() <= MAX_EXTENSION
        && ext
            .iter()
            .all(|&c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}

/// Limits for untrusted input (see the Python `decompress`), and how many blocks to decode
/// at once.
#[derive(Clone, Copy, Debug)]
pub struct DecodeOptions {
    pub max_size: Option<u64>,
    pub max_block_size: Option<u64>,
    /// Blocks decoded at once, each in its own thread (1 = no threads; WebAssembly always
    /// uses 1). Memory grows with it: about 20 times the block size per worker.
    pub workers: usize,
}

impl Default for DecodeOptions {
    fn default() -> Self {
        DecodeOptions {
            max_size: None,
            max_block_size: Some(DEFAULT_MAX_BLOCK_SIZE),
            workers: 1,
        }
    }
}

pub struct Header {
    pub block_size: u64,
    pub extension: Option<String>,
}

pub fn read_header<S: Source>(r: &mut S) -> Result<Header> {
    if r.bytes(3)? != MAGIC {
        return format("not a .paw file");
    }
    let version = r.byte()?;
    if version != FORMAT_VERSION {
        return format(format!("unsupported .paw format version {version}"));
    }
    let block_size = r.varint()?;
    if !(1..=FORMAT_MAX_BLOCK_SIZE).contains(&block_size) {
        return format("invalid block size");
    }
    let length = r.byte()? as usize;
    if length > MAX_EXTENSION {
        return format("stored extension too long");
    }
    let ext = r.bytes(length as u64)?;
    if !extension_allowed(&ext) {
        return format("stored extension has characters that are not allowed");
    }
    let extension = (!ext.is_empty()).then(|| String::from_utf8_lossy(&ext).into_owned());
    let extra = r.varint()?; // optional data a decoder doesn't need: skipped
    if extra > MAX_HEADER_EXTRA {
        return format("optional header data too long");
    }
    r.skip(extra)?;
    Ok(Header {
        block_size,
        extension,
    })
}

pub struct BlockHeader {
    pub original_size: u64,
    pub mode: Mode,
    pub crc32: u32,
    pub body_size: u64,
}

/// Read the next block header, or None after a valid end record.
pub fn next_block<S: Source>(
    r: &mut S,
    block_size: u64,
    total: &mut u64,
    limits: DecodeOptions,
) -> Result<Option<BlockHeader>> {
    let n = r.varint()?;
    if n == 0 {
        if r.varint()? != *total {
            return format("total size does not match the blocks");
        }
        if !r.at_end()? {
            return format("unexpected data after the end of the .paw stream");
        }
        return Ok(None);
    }
    if n > block_size {
        return format("block larger than the declared block size");
    }
    if let Some(max) = limits.max_block_size {
        if n > max {
            return Err(Error::SizeLimit(format!(
                "a block holds {} bytes, more than max_block_size={}",
                group_thousands(n),
                group_thousands(max)
            )));
        }
    }
    *total += n;
    if let Some(max) = limits.max_size {
        if *total > max {
            return Err(Error::SizeLimit(format!(
                "data decompresses to more than max_size={} bytes",
                group_thousands(max)
            )));
        }
    }
    let Some(mode) = Mode::from_byte(r.byte()?) else {
        return format("unknown block mode");
    };
    let crc = u32::from_le_bytes(r.bytes(4)?.try_into().unwrap());
    let body_size = r.varint()?;
    Ok(Some(BlockHeader {
        original_size: n,
        mode,
        crc32: crc,
        body_size,
    }))
}

pub fn decompress_block(mode: Mode, body: &[u8], n: usize) -> Result<Vec<u8>> {
    if mode == Mode::Stored {
        if body.len() != n {
            return format("stored block has the wrong length");
        }
        return Ok(body.to_vec());
    }
    let mut r = Reader::new(body);
    let mut distance = 0;
    if mode.uses_delta() {
        distance = r.varint()?;
        if distance < 1 {
            return format("invalid delta distance");
        }
    }
    let mut data = if mode.uses_bwt() {
        let primary = r.varint()?;
        if primary == 0 || primary > n as u64 {
            return format("invalid BWT index");
        }
        let mut rows = Vec::with_capacity(walk_count(n) - 1);
        for _ in 1..walk_count(n) {
            let row = r.varint()?;
            if row == 0 || row > n as u64 {
                return format("invalid BWT walk row");
            }
            rows.push(row as usize);
        }
        let mut last = decode_map(&body[r.pos..], n)?;
        if mode.uses_mtf() {
            last = inverse_mtf(&last);
        }
        match inverse_bwt(&last, primary as usize, &rows) {
            Some(data) => data,
            None => return format("stored BWT walk row does not match the data"),
        }
    } else {
        decode_map(&body[r.pos..], n)?
    };
    inverse_delta(&mut data, distance);
    if mode.uses_x86() {
        x86_calls(&mut data, false);
    }
    Ok(data)
}

fn decompress_checked(block: &BlockHeader, body: &[u8]) -> Result<Vec<u8>> {
    let data = decompress_block(block.mode, body, block.original_size as usize)?;
    if crc32(&data) != block.crc32 {
        return format("checksum mismatch: the file is damaged or was written incorrectly");
    }
    Ok(data)
}

/// Decompress a complete .paw file held in memory.
pub fn decompress(blob: &[u8], options: DecodeOptions) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    decompress_stream(blob, &mut out, options, &mut |_| {})?;
    Ok(out)
}

/// Decompress a .paw stream from `src` into `dst`, block by block; returns the bytes
/// written. `progress` gets the number of .paw bytes read so far after each block, and once
/// at the end.
///
/// With several workers, the headers of the next few blocks are read first and their bodies
/// decoded at once; errors are still reported in file order, as one worker would.
pub fn decompress_stream<R: Read, W: Write>(
    src: R,
    mut dst: W,
    options: DecodeOptions,
    progress: &mut dyn FnMut(u64),
) -> Result<u64> {
    let mut r = StreamSource::new(src);
    let header = read_header(&mut r)?;
    let workers = if cfg!(target_family = "wasm") {
        1
    } else {
        options.workers.max(1)
    };
    let mut total = 0;
    let mut written = 0u64;
    loop {
        let mut group = Vec::with_capacity(workers);
        let mut stop = Ok(false); // after this group: Ok(true) at the end, or a framing error
        while group.len() < workers {
            match next_block(&mut r, header.block_size, &mut total, options) {
                Ok(Some(block)) => match r.bytes(block.body_size) {
                    Ok(body) => group.push((block, body, r.consumed())),
                    Err(e) => {
                        stop = Err(e);
                        break;
                    }
                },
                Ok(None) => {
                    stop = Ok(true);
                    break;
                }
                Err(e) => {
                    stop = Err(e);
                    break;
                }
            }
        }
        let decoded: Vec<Result<Vec<u8>>> = if group.len() == 1 {
            vec![decompress_checked(&group[0].0, &group[0].1)]
        } else {
            std::thread::scope(|scope| {
                let handles: Vec<_> = group
                    .iter()
                    .map(|(block, body, _)| scope.spawn(move || decompress_checked(block, body)))
                    .collect();
                handles.into_iter().map(|h| h.join().unwrap()).collect()
            })
        };
        for (data, (_, _, consumed)) in decoded.into_iter().zip(&group) {
            let data = data?;
            dst.write_all(&data).map_err(|e| Error::Io(e.to_string()))?;
            written += data.len() as u64;
            progress(*consumed);
        }
        if stop? {
            dst.flush().map_err(|e| Error::Io(e.to_string()))?;
            progress(r.consumed());
            return Ok(written);
        }
    }
}

/// Facts about a .paw file, read from its headers without decompressing it.
#[derive(Debug, Clone)]
pub struct Info {
    pub original_size: u64,
    pub compressed_size: u64,
    pub block_size: u64,
    pub modes: Vec<Mode>,
    pub extension: Option<String>,
}

pub fn info(blob: &[u8]) -> Result<Info> {
    info_stream(blob)
}

/// [`info`] for a .paw stream: reads the headers and skips the block bodies.
pub fn info_stream<R: Read>(src: R) -> Result<Info> {
    let mut r = StreamSource::new(src);
    let header = read_header(&mut r)?;
    let mut total = 0;
    let mut modes = Vec::new();
    let limits = DecodeOptions {
        max_size: None,
        max_block_size: None,
        workers: 1,
    };
    while let Some(block) = next_block(&mut r, header.block_size, &mut total, limits)? {
        r.skip(block.body_size)?;
        modes.push(block.mode);
    }
    Ok(Info {
        original_size: total,
        compressed_size: r.consumed(),
        block_size: header.block_size,
        modes,
        extension: header.extension,
    })
}
