//! pawzip: a position-map + series compressor that writes .paw files.
//!
//! This crate implements the format described in `spec/FORMAT.md`; the Python package in
//! `python/` is the reference implementation, and both must decode the shared test vectors in
//! `spec/vectors/` exactly.

pub mod arith;
pub mod container;
pub mod crc32;
pub mod encoder;
pub mod error;
pub mod freeslots;
pub mod mapcodec;
pub mod numbers;
pub mod series;
pub mod transforms;
pub mod varint;

pub use container::{decompress, decompress_stream, info, info_stream, DecodeOptions, Info, Mode};
pub use encoder::{compress, compress_stream, Options};
pub use error::{Error, Result};
