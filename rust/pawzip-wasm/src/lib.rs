//! The pawzip core compiled to WebAssembly: the engine of the npm package (`js/`).
//!
//! `js/src/index.js` is the public API. It checks the options and fills in their defaults
//! before calling these, and turns the errors thrown here into its own classes, which it
//! recognises by the `name` that [`error`] gives them.

use pawzip::{CompressOptions, DecompressOptions, Error, Mode};
use wasm_bindgen::prelude::*;

/// A pawzip error as a JavaScript `Error`, named for the class `js/src/index.js` throws. The
/// messages name the options as JavaScript spells them.
fn error(e: Error) -> JsValue {
    let name = match e {
        Error::Format(_) => "PawFormatError",
        Error::SizeLimit(_) => "PawSizeLimitError",
        Error::Invalid(_) => "PawInvalidError",
        _ => "PawError",
    };
    let message = e
        .to_string()
        .replace("max_block_size=", "maxBlockSize=")
        .replace("max_size=", "maxSize=");
    let error = js_sys::Error::new(&message);
    error.set_name(name);
    error.into()
}

fn compress_options(
    block_size: f64,
    modes: Option<Vec<u8>>,
    extension: Option<String>,
    thorough: bool,
) -> Result<CompressOptions, JsValue> {
    let mut options = CompressOptions::default();
    options.block_size = block_size as u64;
    if let Some(modes) = modes {
        let modes = modes.into_iter().map(|byte| {
            Mode::from_byte(byte)
                .ok_or_else(|| error(Error::Invalid(format!("unknown mode: {byte}"))))
        });
        options.modes = Some(modes.collect::<Result<_, _>>()?);
    }
    options.extension = extension;
    options.thorough = thorough;
    Ok(options)
}

/// `None` means no limit, for both.
fn decompress_options(max_size: Option<f64>, max_block_size: Option<f64>) -> DecompressOptions {
    let mut options = DecompressOptions::default();
    options.max_size = max_size.map(|limit| limit as u64);
    options.max_block_size = max_block_size.map(|limit| limit as u64);
    options
}

/// Compress data into a complete .paw file.
#[wasm_bindgen]
pub fn compress(
    data: &[u8],
    block_size: f64,
    modes: Option<Vec<u8>>,
    extension: Option<String>,
    thorough: bool,
) -> Result<Vec<u8>, JsValue> {
    let options = compress_options(block_size, modes, extension, thorough)?;
    pawzip::compress(data, &options).map_err(error)
}

/// Decompress a complete .paw file.
#[wasm_bindgen]
pub fn decompress(
    data: &[u8],
    max_size: Option<f64>,
    max_block_size: Option<f64>,
) -> Result<Vec<u8>, JsValue> {
    pawzip::decompress(data, &decompress_options(max_size, max_block_size)).map_err(error)
}

/// The facts in a .paw file's headers, without decompressing it.
#[wasm_bindgen]
pub fn info(data: &[u8]) -> Result<js_sys::Object, JsValue> {
    let info = pawzip::info(data).map_err(error)?;
    let modes = js_sys::Array::new();
    for mode in &info.modes {
        modes.push(&JsValue::from_str(mode.label()));
    }
    let extension = info
        .extension
        .as_deref()
        .map_or(JsValue::NULL, JsValue::from_str);
    let object = js_sys::Object::new();
    for (key, value) in [
        ("originalSize", JsValue::from_f64(info.original_size as f64)),
        (
            "compressedSize",
            JsValue::from_f64(info.compressed_size as f64),
        ),
        ("blockSize", JsValue::from_f64(info.block_size as f64)),
        ("modes", modes.into()),
        ("extension", extension),
    ] {
        js_sys::Reflect::set(&object, &JsValue::from_str(key), &value)?;
    }
    Ok(object)
}

/// Compresses input given piece by piece (behind `CompressStream`).
#[wasm_bindgen]
pub struct Compressor(pawzip::internal::Compressor);

#[wasm_bindgen]
impl Compressor {
    #[wasm_bindgen(constructor)]
    pub fn new(
        block_size: f64,
        modes: Option<Vec<u8>>,
        extension: Option<String>,
        thorough: bool,
    ) -> Result<Compressor, JsValue> {
        let options = compress_options(block_size, modes, extension, thorough)?;
        pawzip::internal::Compressor::new(options)
            .map(Compressor)
            .map_err(error)
    }

    /// The .paw bytes ready after this input: the header first, then each finished block (at
    /// most one, for input no larger than a block).
    pub fn push(&mut self, data: &[u8]) -> Vec<u8> {
        self.0.push(data)
    }

    /// The rest of the stream. Frees the compressor.
    pub fn finish(self) -> Vec<u8> {
        self.0.finish()
    }

    /// Input bytes compressed so far.
    #[wasm_bindgen(getter)]
    pub fn done(&self) -> f64 {
        self.0.done() as f64
    }
}

/// Decompresses a .paw stream given piece by piece (behind `DecompressStream`).
#[wasm_bindgen]
pub struct Decompressor(pawzip::internal::Decompressor);

#[wasm_bindgen]
impl Decompressor {
    #[wasm_bindgen(constructor)]
    pub fn new(max_size: Option<f64>, max_block_size: Option<f64>) -> Decompressor {
        Decompressor(pawzip::internal::Decompressor::new(decompress_options(
            max_size,
            max_block_size,
        )))
    }

    /// Takes the next piece of the .paw stream.
    pub fn push(&mut self, data: &[u8]) -> Result<(), JsValue> {
        self.0.push(data).map_err(error)
    }

    /// The original bytes of the next block, once all of it has arrived; `undefined` until
    /// then, and after the end.
    pub fn next_block(&mut self) -> Result<Option<Vec<u8>>, JsValue> {
        self.0.next_block().map_err(error)
    }

    /// Checks that the stream ended properly. Frees the decompressor.
    pub fn finish(self) -> Result<(), JsValue> {
        self.0.finish().map_err(error)
    }

    /// .paw bytes used so far.
    #[wasm_bindgen(getter)]
    pub fn consumed(&self) -> f64 {
        self.0.consumed() as f64
    }
}
