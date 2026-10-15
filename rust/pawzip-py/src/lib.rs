//! `paw._core`: the block encoder and decoder of the Rust core, for the Python package.
//!
//! Only the per-block work lives here; the container, streaming, files and the command line
//! stay in Python (`paw.blocks` calls these functions when the module is available). Both
//! release the GIL, so blocks can be coded in parallel threads.

use pawzip::{Error, Mode};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

fn mode(byte: u8) -> PyResult<Mode> {
    Mode::from_byte(byte).ok_or_else(|| PyValueError::new_err(format!("unknown mode: {byte}")))
}

/// Raise the Python package's own error for a decoding failure.
fn to_py(py: Python<'_>, e: Error) -> PyErr {
    let name = match e {
        Error::SizeLimit(_) => "SizeLimitError",
        _ => "FormatError",
    };
    match py
        .import("paw.errors")
        .and_then(|m| m.getattr(name))
        .and_then(|cls| cls.call1((e.to_string(),)))
    {
        Ok(err) => PyErr::from_value(err),
        Err(import_failed) => import_failed,
    }
}

/// Encode one non-empty block: (mode, body), the smallest of the candidate modes.
#[pyfunction]
#[pyo3(signature = (data, modes=None, thorough=false))]
fn compress_block<'py>(
    py: Python<'py>,
    data: &[u8],
    modes: Option<Vec<u8>>,
    thorough: bool,
) -> PyResult<(u8, Bound<'py, PyBytes>)> {
    let modes = modes
        .map(|m| m.into_iter().map(mode).collect::<PyResult<Vec<_>>>())
        .transpose()?;
    let (mode, body) =
        py.detach(|| pawzip::internal::compress_block(data, modes.as_deref(), thorough));
    Ok((mode as u8, PyBytes::new(py, &body)))
}

/// Decode one block body of `n` original bytes.
#[pyfunction]
fn decompress_block<'py>(
    py: Python<'py>,
    mode_byte: u8,
    body: &[u8],
    n: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    let m = mode(mode_byte)?;
    match py.detach(|| pawzip::internal::decompress_block(m, body, n)) {
        Ok(data) => Ok(PyBytes::new(py, &data)),
        Err(e) => Err(to_py(py, e)),
    }
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compress_block, m)?)?;
    m.add_function(wrap_pyfunction!(decompress_block, m)?)?;
    Ok(())
}
