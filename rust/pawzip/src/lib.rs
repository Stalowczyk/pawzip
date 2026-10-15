#![doc = include_str!("../README.md")]
#![forbid(unsafe_code)]
#![warn(missing_docs)]

// The format is spec/FORMAT.md. The Python package (python/src/paw) is the reference
// implementation: this crate writes exactly the same files, and both must decode the shared
// test vectors in spec/vectors/.

mod container;
mod encoder;
mod error;
mod varint;

// Some items of these reach other crates through the hidden `internal` module, undocumented.
#[allow(missing_docs)]
mod arith;
#[allow(missing_docs)]
mod crc32;
#[allow(missing_docs)]
mod freeslots;
#[allow(missing_docs)]
mod mapcodec;
#[allow(missing_docs)]
mod numbers;
#[allow(missing_docs)]
mod series;
#[allow(missing_docs)]
mod transforms;

pub use container::{
    decompress, decompress_stream, extension_allowed, info, info_stream, read_header,
    DecompressOptions, Header, Info, Mode, DEFAULT_BLOCK_SIZE, DEFAULT_MAX_BLOCK_SIZE,
    FORMAT_MAX_BLOCK_SIZE, FORMAT_VERSION, MAGIC, MAX_EXTENSION,
};
pub use encoder::{compress, compress_stream, CompressOptions};
pub use error::{Error, Result};

/// Parts of the implementation used by pawzip's own bindings (the Python package's native
/// module) and examples. Not part of the public API: they can change in any release.
#[doc(hidden)]
pub mod internal {
    pub use crate::arith::{Decoder, Encoder, Model};
    pub use crate::container::decompress_block;
    pub use crate::crc32::crc32;
    pub use crate::encoder::compress_block;
    pub use crate::freeslots::FreeSlots;
    pub use crate::mapcodec::{decode_map, encode_map};
    pub use crate::numbers::{get, put, Contexts};
    pub use crate::series::select_series;
    pub use crate::transforms::{bwt, inverse_bwt, inverse_mtf, mtf};
}
