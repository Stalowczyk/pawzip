//! Unsigned LEB128 varints: 7 bits per byte, lowest bits first, at most 10 bytes and 64 bits.

use crate::error::{format, Result};

pub fn write_varint(out: &mut Vec<u8>, mut n: u64) {
    while n >= 0x80 {
        out.push((n as u8 & 0x7F) | 0x80);
        n >>= 7;
    }
    out.push(n as u8);
}

/// Sequential reader over a byte slice.
pub struct Reader<'a> {
    pub data: &'a [u8],
    pub pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        Reader { data, pos: 0 }
    }

    pub fn byte(&mut self) -> Result<u8> {
        match self.data.get(self.pos) {
            Some(&b) => {
                self.pos += 1;
                Ok(b)
            }
            None => format("unexpected end of data"),
        }
    }

    pub fn take(&mut self, count: u64) -> Result<&'a [u8]> {
        let remaining = (self.data.len() - self.pos) as u64;
        if count > remaining {
            return format("unexpected end of data");
        }
        let start = self.pos;
        self.pos += count as usize;
        Ok(&self.data[start..self.pos])
    }

    pub fn varint(&mut self) -> Result<u64> {
        let mut result: u64 = 0;
        for i in 0..10 {
            let b = self.byte()?;
            let bits = u64::from(b & 0x7F);
            if i == 9 && bits > 1 {
                return format("varint larger than 64 bits");
            }
            result |= bits << (7 * i);
            if b < 0x80 {
                return Ok(result);
            }
        }
        format("varint longer than 10 bytes")
    }

    pub fn at_end(&self) -> bool {
        self.pos >= self.data.len()
    }
}

/// Where container headers are read from: a byte slice ([`Reader`]) or a stream
/// ([`StreamSource`]). Both validate exactly the same way.
pub trait Source {
    fn byte(&mut self) -> Result<u8>;
    /// The next `count` bytes ("unexpected end of data" if there are fewer).
    fn bytes(&mut self, count: u64) -> Result<Vec<u8>>;
    /// Skip `count` bytes ("unexpected end of data" if there are fewer).
    fn skip(&mut self, count: u64) -> Result<()>;
    /// True if nothing follows.
    fn at_end(&mut self) -> Result<bool>;
    /// Bytes read so far.
    fn consumed(&self) -> u64;

    fn varint(&mut self) -> Result<u64> {
        let mut result: u64 = 0;
        for i in 0..10 {
            let b = self.byte()?;
            let bits = u64::from(b & 0x7F);
            if i == 9 && bits > 1 {
                return format("varint larger than 64 bits");
            }
            result |= bits << (7 * i);
            if b < 0x80 {
                return Ok(result);
            }
        }
        format("varint longer than 10 bytes")
    }
}

impl Source for Reader<'_> {
    fn byte(&mut self) -> Result<u8> {
        Reader::byte(self)
    }
    fn bytes(&mut self, count: u64) -> Result<Vec<u8>> {
        Ok(self.take(count)?.to_vec())
    }
    fn skip(&mut self, count: u64) -> Result<()> {
        self.take(count).map(|_| ())
    }
    fn at_end(&mut self) -> Result<bool> {
        Ok(Reader::at_end(self))
    }
    fn consumed(&self) -> u64 {
        self.pos as u64
    }
}

/// A [`Source`] over any reader. Reading a claimed length never allocates more than the
/// data that actually arrives, so a header claiming a huge body fails cleanly at the end.
pub struct StreamSource<R: std::io::Read> {
    inner: std::io::BufReader<R>,
    consumed: u64,
}

impl<R: std::io::Read> StreamSource<R> {
    pub fn new(inner: R) -> Self {
        StreamSource {
            inner: std::io::BufReader::with_capacity(1 << 16, inner),
            consumed: 0,
        }
    }
}

fn io_error(e: std::io::Error) -> crate::error::Error {
    crate::error::Error::Io(e)
}

impl<R: std::io::Read> Source for StreamSource<R> {
    fn byte(&mut self) -> Result<u8> {
        let mut b = [0u8];
        match std::io::Read::read_exact(&mut self.inner, &mut b) {
            Ok(()) => {
                self.consumed += 1;
                Ok(b[0])
            }
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                format("unexpected end of data")
            }
            Err(e) => Err(io_error(e)),
        }
    }

    fn bytes(&mut self, count: u64) -> Result<Vec<u8>> {
        let mut out = Vec::new();
        let got =
            std::io::Read::read_to_end(&mut std::io::Read::take(&mut self.inner, count), &mut out)
                .map_err(io_error)?;
        self.consumed += got as u64;
        if (got as u64) < count {
            return format("unexpected end of data");
        }
        Ok(out)
    }

    fn skip(&mut self, count: u64) -> Result<()> {
        let got = std::io::copy(
            &mut std::io::Read::take(&mut self.inner, count),
            &mut std::io::sink(),
        )
        .map_err(io_error)?;
        self.consumed += got;
        if got < count {
            return format("unexpected end of data");
        }
        Ok(())
    }

    fn at_end(&mut self) -> Result<bool> {
        std::io::BufRead::fill_buf(&mut self.inner)
            .map(|b| b.is_empty())
            .map_err(io_error)
    }

    fn consumed(&self) -> u64 {
        self.consumed
    }
}
