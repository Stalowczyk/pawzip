# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-10-17

### Added

- The Rust library and the `paw` command are now also on crates.io, as `pawzip` and
  `pawzip-cli`.

### Changed

- The Python package's native module is built with PyO3 0.30 (was 0.23).
- New documentation: a shorter README, a How it works page with diagrams, a contributing
  guide, and clearer PyPI and crates.io pages.

## [1.0.0] - 2026-10-15

The first release, 1.0.0.

### Added

- **The `.paw` format, version 1 (frozen).** Every file written by this or any later
  version decodes to the same bytes with every later version. The full byte-level
  specification is `spec/FORMAT.md`.
  - Data is cut into independent blocks (16 MiB by default), each with its own mode and a
    CRC-32 of the original bytes.
  - Each block is stored as a position map: a background byte, then for every other
    byte value its positions, described by arithmetic and nested-loop series wherever
    they pay off, and the rest as gaps between free slots, all coded with an adaptive
    range coder.
  - Eight modes, chosen per block: stored, the map of the raw data, or the map after a
    Burrows–Wheeler transform (with or without move-to-front), a delta filter for smooth
    images and signals, or an x86 call filter for executables.
  - The original file's extension is stored, so `photo.png` → `photo.paw` → `photo.png`,
    even after renaming the `.paw` file.
- **Test vectors** in `spec/vectors/` for anyone writing an implementation: every mode on a
  range of inputs, valid files the reference encoder never writes (`unusual/`), and one
  invalid file per validation rule (`invalid/`).
- **Python package** `pawzip` (import `paw`), with a Rust core (abi3 wheels for Python
  3.10+) and a pure-Python reference implementation that writes identical files
  (`PAW_PURE=1`).
  - `compress`, `decompress`, `info`, `compress_file`, `decompress_file`, streaming with
    `paw.open`, `compress_stream` and `decompress_stream`, the `Mode` enum, and the errors
    `PawError`, `FormatError` and `SizeLimitError`. Fully typed.
  - Parallel compression and decompression with `workers=` (threads; `"auto"` fits the CPU
    and the free memory), and `progress=` callbacks.
  - Safe on untrusted input: `max_size=` and `max_block_size=` (64 MiB by default) are
    checked from the headers before anything large is allocated.
  - Already-compressed data (JPEG, ZIP, video) is detected from a sample and stored at
    once; `thorough=True` tries every mode anyway.
- **Command line** `paw`: `compress`, `decompress`, `test` and `info`; several files per
  call, stdin/stdout, `--mode`, `--block-size`, `-T/--workers`, `--thorough`,
  `--max-size`, `--max-block-size`, a progress line on terminals, and partial output
  removed on errors and Ctrl-C.
- **Standalone Rust command** (`rust/pawzip-cli`): the same `paw` command, options,
  messages and files, without Python.
