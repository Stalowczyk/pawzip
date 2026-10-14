//! Errors, matching the Python package: `FormatError` and `SizeLimitError`.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// Not a valid .paw file: wrong magic, unsupported version, damaged or truncated data,
    /// or a checksum mismatch.
    Format(String),
    /// The data would be larger than the caller allowed (`max_size` or `max_block_size`).
    SizeLimit(String),
    /// Reading or writing failed (streaming only).
    Io(String),
    /// An option is out of range (streaming only).
    Invalid(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Format(m) | Error::SizeLimit(m) | Error::Io(m) | Error::Invalid(m) => {
                f.write_str(m)
            }
        }
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

/// 1234567 -> "1,234,567" (as Python's `f"{n:,}"`, used in messages).
pub fn group_thousands(n: u64) -> String {
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
