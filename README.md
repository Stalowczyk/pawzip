# pawzip

pawzip is a lossless compressor with its own file format, `.paw`. Instead of storing a file
byte by byte, it stores where each byte value appears, and describes those positions with
series (arithmetic progressions and nested loops) wherever it can.

On the Silesia benchmark it compresses better than bzip2 and nearly as well as xz, but it is
slower than both. It does best on structured and repetitive data. Data it can't compress,
such as noise or files that are already compressed, is stored as it is.

## Install

```
pip install pawzip
paw compress notes.txt          # -> notes.paw (remembers .txt)
paw decompress notes.paw        # -> notes.txt
```

```python
import paw

blob = paw.compress(b"hello world\n" * 1000)
assert paw.decompress(blob) == b"hello world\n" * 1000
```

The full Python API, command line and limitations are documented in
[python/README.md](python/README.md), which is also the package's PyPI page.

For Rust, the library is [`pawzip` on crates.io](https://crates.io/crates/pawzip)
([documentation](https://docs.rs/pawzip)). The same `paw` command also comes as a standalone
Rust binary, without Python: `cargo install pawzip-cli` (same options, messages and files).

## How it compares

The [Silesia corpus](https://sun.aei.polsl.pl//~sdeor/index.php?page=silesia) is a standard
compression benchmark: 12 files, 212 MB of text, databases, executables, images and XML.
Each file compressed separately:

|          |  size | compression | decompression |
|----------|------:|------------:|--------------:|
| gzip -9  | 31.9% |     10 MB/s |      240 MB/s |
| bzip2 -9 | 25.7% |     11 MB/s |       25 MB/s |
| paw      | 23.8% |    1.7 MB/s |       18 MB/s |
| xz -9    | 23.0% |    1.9 MB/s |       62 MB/s |

Size is the total compressed size as a percentage of the original (lower is better). paw is
smaller than xz on 9 of the 12 files. xz is smaller in total, mostly because of `mozilla`
and `samba` (tar archives of programs and of source code), which make up a third of the
corpus. Speeds are for one core of an Intel Core i3-12100F under Windows 10, with paw 1.0.0
at its default settings and the others through Python's gzip, bz2 and lzma modules. paw
tries its modes in parallel, so with all four cores available it compresses at about
3.5 MB/s.

## How it works

1. **Position map**: every byte value gets the sorted list of positions where it occurs.
2. **Background byte**: the most frequent byte value is never stored. It fills every gap.
3. **Series**: positions that form arithmetic progressions (`start, step, count`) or nested
   loops (`start + row·stride + col·inner`) are replaced by a few numbers. Series are chosen
   greedily, and only when they actually save space. Their shapes are remembered, so a series
   that repeats a recent shape (the same rectangle somewhere else) only stores its start.
4. **Leftovers**: the remaining positions are stored as gaps, compressed with adaptive
   arithmetic coding. Gaps only count free slots: positions already claimed by earlier byte
   values are skipped, since they can't hold this one.
5. **Modes**: each block is compressed several ways and the smallest result is kept. A block
   can be stored as it is, mapped directly, or mapped after a reversible transform: a
   Burrows–Wheeler transform with or without move-to-front (good for text), a delta filter
   (good for smooth images and signals), both of these, or an x86 call filter followed by the
   Burrows–Wheeler transform (good for executables). The delta and x86 filters are only tried
   when the data looks suitable.

Every block carries a CRC-32 of its original bytes, so a damaged file is detected instead of
being decoded into garbage.

## The format

Version 1 of the `.paw` format is frozen: every file written today will decode to the same
bytes with every later version. The complete byte-level specification is
[spec/FORMAT.md](spec/FORMAT.md), so anyone can write their own decoder, and the test vectors
in [spec/vectors/](spec/vectors/) check it: files in every mode, valid files the reference
encoder never writes, and one invalid file per validation rule.

## Standard building blocks

The position map, background byte, series and free-slot gaps are this project's own approach.
Around them it uses well-known techniques:

- the Burrows–Wheeler transform (Burrows and Wheeler, 1994), the reshaping step used by
  bzip2, optionally followed by move-to-front
- adaptive range coding, the byte-wise form of arithmetic coding (Martin, 1979) used by LZMA
  and 7-Zip
- delta filtering, as used by PNG and many audio formats
- the x86 call filter ("E8/E9" address translation) for executables, as in 7-Zip's BCJ and
  xz's x86 filter
- LEB128 varints and CRC-32 for the container

## Repository layout

```
spec/          the .paw format spec + test vectors every implementation must pass
python/        the Python package (`pawzip`, import `paw`): API, CLI and the pure-Python reference
rust/          the Rust library (pawzip), the package's native module (pawzip-py) and a
               standalone `paw` command (pawzip-cli)
```

## Development

```
python -m venv .venv
.venv/bin/python -m pip install -e "python[dev]"   # Scripts/ instead of bin/ on Windows
                                                   # (needs Rust: builds paw._core; run it
                                                   # again after changing Rust code)
.venv/bin/pre-commit install                       # lint + format on every commit

.venv/bin/python -m pytest python                  # all tests, on the Rust core
PAW_PURE=1 .venv/bin/python -m pytest python       # the pure-Python reference
cd rust && cargo test --release -p pawzip -p pawzip-cli        # the Rust tests
```

The pure-Python implementation is the reference: the Rust core writes exactly the same
files, which `rust/differential.py` and the shared vectors check.

## License

MIT, see [LICENSE](LICENSE). Changes are listed in [CHANGELOG.md](CHANGELOG.md).
