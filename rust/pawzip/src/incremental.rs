//! Compressing and decompressing data that arrives in pieces, for bindings whose input comes
//! in chunks and can't be read on demand, such as JavaScript streams (the npm package).
//!
//! The results are those of [`compress_stream`](crate::compress_stream) and
//! [`decompress_stream`](crate::decompress_stream), wherever the input is cut: the same .paw
//! bytes, the same output, and the same errors after the same output.

use crate::container::{decompress_checked, parse_header, DecompressOptions};
use crate::encoder::{check_options, encode_block, end, header, CompressOptions};
use crate::error::{format, Error, Result};
use crate::varint::Source;

/// Compresses input given piece by piece into the .paw stream that
/// [`compress_stream`](crate::compress_stream) writes for the same input and options.
pub struct Compressor {
    options: CompressOptions,
    extension: Option<String>,
    started: bool,
    /// Input not yet in a block: always less than the block size.
    pending: Vec<u8>,
    done: u64,
}

impl Compressor {
    /// Fails with [`Error::Invalid`] when an option is out of range, like
    /// [`compress`](crate::compress).
    pub fn new(options: CompressOptions) -> Result<Self> {
        let extension = check_options(&options).map_err(Error::Invalid)?;
        Ok(Compressor {
            options,
            extension,
            started: false,
            pending: Vec::new(),
            done: 0,
        })
    }

    /// Takes the next piece of input and returns the .paw bytes that are ready: the header the
    /// first time, then each block that the input completes. A piece of at most the block size
    /// completes at most one block.
    pub fn push(&mut self, mut data: &[u8]) -> Vec<u8> {
        let mut out = self.start();
        let size = self.options.block_size as usize;
        while !data.is_empty() {
            if self.pending.is_empty() && data.len() >= size {
                // A whole block straight from the input, without copying it first.
                let (block, rest) = data.split_at(size);
                self.encode(block, &mut out);
                data = rest;
                continue;
            }
            let take = (size - self.pending.len()).min(data.len());
            self.pending.extend_from_slice(&data[..take]);
            data = &data[take..];
            if self.pending.len() == size {
                let block = std::mem::take(&mut self.pending);
                self.encode(&block, &mut out);
                self.pending = block;
                self.pending.clear();
            }
        }
        out
    }

    /// Ends the input and returns the rest of the stream: the last block, if one is
    /// unfinished, and the end record.
    pub fn finish(mut self) -> Vec<u8> {
        let mut out = self.start();
        if !self.pending.is_empty() {
            let block = std::mem::take(&mut self.pending);
            self.encode(&block, &mut out);
        }
        out.extend(end(self.done));
        out
    }

    /// Input bytes compressed so far: those in the blocks returned.
    pub fn done(&self) -> u64 {
        self.done
    }

    fn start(&mut self) -> Vec<u8> {
        if self.started {
            return Vec::new();
        }
        self.started = true;
        header(self.options.block_size, self.extension.as_deref())
    }

    fn encode(&mut self, block: &[u8], out: &mut Vec<u8>) {
        out.extend(encode_block(
            block,
            self.options.modes.as_deref(),
            self.options.thorough,
        ));
        self.done += block.len() as u64;
    }
}

/// Decompresses a .paw stream given piece by piece, as
/// [`decompress_stream`](crate::decompress_stream) does: [`push`](Self::push) the input as it
/// arrives, and take the blocks it completes from [`next_block`](Self::next_block), one at a
/// time.
///
/// Only the input not decoded yet is kept, and a block is decoded only when it's asked for, so
/// memory stays bounded by the block size however much the data expands. After an error the
/// stream can't continue: drop it.
pub struct Decompressor {
    options: DecompressOptions,
    /// The input received, of which `buffer[..start]` is used.
    buffer: Vec<u8>,
    start: usize,
    /// The file's block size, once its header has been read.
    block_size: Option<u64>,
    /// Original bytes declared by the blocks decoded so far.
    total: u64,
    /// .paw bytes used so far: the header and the blocks decoded.
    consumed: u64,
    /// The end record has been read.
    ended: bool,
}

impl Decompressor {
    /// A decompressor with the limits of `options` (`workers` is ignored: blocks are decoded
    /// one at a time).
    pub fn new(options: DecompressOptions) -> Self {
        Decompressor {
            options,
            buffer: Vec::new(),
            start: 0,
            block_size: None,
            total: 0,
            consumed: 0,
            ended: false,
        }
    }

    /// Takes the next piece of the .paw stream. Fails only for data after the end record.
    pub fn push(&mut self, data: &[u8]) -> Result<()> {
        if data.is_empty() {
            return Ok(());
        }
        if self.ended {
            return format("unexpected data after the end of the .paw stream");
        }
        self.buffer.drain(..self.start);
        self.start = 0;
        self.buffer.extend_from_slice(data);
        Ok(())
    }

    /// The original bytes of the next block, once all of it has arrived; `None` while more
    /// input is needed, and after the end. Every block's CRC-32 is checked, and the limits are
    /// applied as soon as a block's header has arrived.
    pub fn next_block(&mut self) -> Result<Option<Vec<u8>>> {
        while !self.ended {
            let mut src = Partial::new(&self.buffer[self.start..]);
            let Some(block_size) = self.block_size else {
                match parse_header(&mut src) {
                    Ok(header) => self.block_size = Some(header.block_size),
                    Err(_) if src.short => return Ok(None),
                    Err(e) => return Err(e),
                }
                let header_length = src.pos;
                self.used(header_length);
                continue;
            };
            // A block header can be cut anywhere, so it's read again in full each time, and
            // the running total is updated only once the whole block is here.
            let mut total = self.total;
            let read = crate::container::next_block(&mut src, block_size, &mut total, self.options);
            let block = match read {
                Ok(Some(block)) => block,
                Ok(None) => {
                    // The end record, with the total checked and nothing after it so far.
                    let end_length = src.pos;
                    self.used(end_length);
                    self.ended = true;
                    return Ok(None);
                }
                Err(_) if src.short => return Ok(None),
                Err(e) => return Err(e),
            };
            let body = src.pos;
            if ((src.data.len() - body) as u64) < block.body_size {
                return Ok(None);
            }
            let length = body + block.body_size as usize;
            let data = decompress_checked(&block, &src.data[body..length])?;
            self.used(length);
            self.total = total;
            return Ok(Some(data));
        }
        Ok(None)
    }

    /// Ends the input, after [`next_block`](Self::next_block) has given every block: an error
    /// if the stream stopped before its end record.
    pub fn finish(self) -> Result<()> {
        if self.ended {
            Ok(())
        } else {
            format("unexpected end of data")
        }
    }

    /// .paw bytes used so far: the header and every block returned.
    pub fn consumed(&self) -> u64 {
        self.consumed
    }

    fn used(&mut self, length: usize) {
        self.start += length;
        self.consumed += length as u64;
    }
}

/// A [`Source`] over the input received so far, which notes when a read runs past its end:
/// then the data may only be incomplete, rather than invalid.
struct Partial<'a> {
    data: &'a [u8],
    pos: usize,
    short: bool,
}

impl<'a> Partial<'a> {
    fn new(data: &'a [u8]) -> Self {
        Partial {
            data,
            pos: 0,
            short: false,
        }
    }

    fn advance(&mut self, count: u64) -> Result<usize> {
        if count > (self.data.len() - self.pos) as u64 {
            self.short = true;
            return format("unexpected end of data");
        }
        let start = self.pos;
        self.pos += count as usize;
        Ok(start)
    }
}

impl Source for Partial<'_> {
    fn byte(&mut self) -> Result<u8> {
        let start = self.advance(1)?;
        Ok(self.data[start])
    }

    fn bytes(&mut self, count: u64) -> Result<Vec<u8>> {
        let start = self.advance(count)?;
        Ok(self.data[start..self.pos].to_vec())
    }

    fn skip(&mut self, count: u64) -> Result<()> {
        self.advance(count).map(|_| ())
    }

    fn at_end(&mut self) -> Result<bool> {
        Ok(self.pos >= self.data.len())
    }

    fn consumed(&self) -> u64 {
        self.pos as u64
    }
}
