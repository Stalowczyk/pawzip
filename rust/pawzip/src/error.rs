//! Errors, matching the Python package: `FormatError` and `SizeLimitError`.

use std::fmt;

/// Why compressing or decompressing failed.
#[derive(Debug)]
#[non_exhaustive]
pub enum Error {
    /// Not a valid .paw file: wrong magic, unsupported version, damaged or truncated data,
    /// or a checksum mismatch.
    Format(String),
    /// The data would be larger than the caller allowed
    /// ([`max_size`](crate::DecompressOptions::max_size) or
    /// [`max_block_size`](crate::DecompressOptions::max_block_size)).
    SizeLimit(String),
    /// Reading or writing a stream failed.
    Io(std::io::Error),
    /// An option is out of range.
    Invalid(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Format(m) | Error::SizeLimit(m) | Error::Invalid(m) => f.write_str(m),
            Error::Io(e) => fmt::Display::fmt(e, f),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

/// `Result` with this crate's [`Error`].
pub type Result<T> = std::result::Result<T, Error>;

/// 1234567 -> "1,234,567" (as Python's `f"{n:,}"`, used in messages).
pub(crate) fn group_thousands(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

pub(crate) fn format<T>(message: impl Into<String>) -> Result<T> {
    Err(Error::Format(message.into()))
}
