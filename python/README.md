# pawzip

pawzip is a lossless compressor that writes `.paw` files. Instead of storing data byte by
byte, it records where each byte value appears and describes those positions with arithmetic
series and nested loops where it can, storing the rest as compactly coded gaps.

On the Silesia benchmark it compresses better than bzip2 and nearly as well as xz, but it is
slower than both (see Limitations). It does best on structured and repetitive data, and data
it can't compress, such as noise or files that are already compressed, is stored as it is.

```
pip install pawzip
```

Requires Python 3.10+ and NumPy. The wheels include the compressor's core compiled from Rust;
installing from source needs a Rust toolchain ([rustup.rs](https://rustup.rs)). Setting the
environment variable `PAW_PURE=1` switches to the pure-Python reference implementation
(same output, much slower).

## Python API

```python
import paw

data = b"hello world\n" * 1000
blob = paw.compress(data)  # bytes -> .paw bytes
assert paw.decompress(blob) == data  # .paw bytes -> the original bytes

meta = paw.info(blob)  # read the headers without decompressing (a path works too)
print(meta.original_size, meta.compressed_size, meta.ratio, meta.mode_summary)

paw.compress_file("notes.txt")  # writes notes.paw, remembering ".txt"
paw.decompress_file("notes.paw", "copy.txt")  # without a destination: notes.txt

with paw.open("log.paw", "wt", encoding="utf-8", extension="txt") as f:  # like gzip.open
    f.write("hello\n")
```

- `compress(data, modes=None)` tries every suitable mode and keeps the smallest result. Pass
  `modes=[paw.Mode.MAP]` (or another `paw.Mode`) to try only those modes, which is faster.
- `decompress(blob, max_size=None)` verifies a CRC-32 of every block. **For untrusted
  input, pass `max_size`**: a tiny file can claim an enormous size, and `max_size`
  refuses it before any memory is allocated.
- Decoding a block needs about 7x its size in memory (20x in pure Python), so blocks
  larger than `max_block_size` (default 64 MiB) are refused the same way. Files written
  with the default block size are never affected; pass a larger `max_block_size` (CLI
  `--max-block-size`) for files written with bigger blocks.
- File names: `photo.png` compresses to `photo.paw`, and the extension `png` is stored
  inside it. Decompressing gives back `photo.png`, or `abc.png` if you renamed the file to
  `abc.paw`. Only the extension is stored, never the name. For `compress` and
  `compress_stream`, pass `extension="png"` yourself.
- Already-compressed data (JPEG, ZIP, video) is recognised from a small sample and stored
  as it is, in a fraction of a second. `thorough=True` (CLI `--thorough`) tries every mode
  anyway: many times slower, for a percent or so.
- `compress_file` / `decompress_file` never overwrite an existing file unless
  `overwrite=True`, and never leave a partial output file behind on failure.
- Data is split into independent blocks (default 16 MiB, `block_size=`), so files of
  any size can be compressed and decompressed with bounded memory. `compress_file`,
  `decompress_file`, `paw.open`, `compress_stream` and `decompress_stream` all stream.
- `compress_file`, `decompress_file`, `compress_stream` and `decompress_stream` accept a
  `progress` callback: it is called after each block, and once at the end, with the
  number of input bytes processed so far (handy for a progress bar).
- Parallel compression and decompression are opt-in: `workers=4` (CLI `-T 4`), or
  `workers="auto"` to use the CPU cores that fit into half of the currently free memory.
  Files with several blocks gain: Webster in 4 MiB blocks compresses at 13.6 MB/s and
  decompresses at 78 MB/s with 4 workers. The default is a single worker, and `"auto"`
  also uses one where free memory can't be measured (currently macOS; pass a number
  there). Workers are threads; only the pure-Python fallback uses processes, which need
  the usual `if __name__ == "__main__":` guard on Windows/macOS.
- Errors: `paw.FormatError` (not a valid or a damaged .paw file) and `paw.SizeLimitError`
  (over `max_size` or `max_block_size`), both subclasses of `paw.PawError`, which is a
  `ValueError`.

The package is fully type-annotated.

## Command line

```
paw compress notes.txt                 # -> notes.paw
paw decompress notes.paw               # -> notes.txt (also after renaming it)
paw test archive.paw                   # check it decompresses correctly
paw info archive.paw                   # sizes, blocks and modes
cat data.bin | paw compress - > data.bin.paw
paw decompress --max-size 500M -c big.paw > big
paw compress -T auto -B 8M huge.iso    # parallel, 8 MiB blocks
paw compress --thorough photo.jpg      # squeeze already-compressed files too (slow)
```

When stderr is a terminal, a progress line shows how far each file has got and roughly how
long is left (`-q` hides it). Run `paw COMMAND --help` for all options. Exit status is 0 on
success, 1 if any file failed, 2 on usage errors and 130 if interrupted.

## Limitations

- **Speed** (Rust core, Silesia benchmark): on one core, compression runs at about
  1.7 MB/s, close to xz -9, and decompression at about 18 MB/s, a little slower than bzip2
  and about 3x slower than xz. Compression tries its modes in parallel, so with several
  cores it is faster: about 3.5 MB/s with four cores. The pure-Python reference
  (`PAW_PURE=1`) is about 50x slower.
- **Memory**: compressing needs up to about 100x the block size per worker (about 1.6 GB
  for a full 16 MiB block, when several modes are tried at once); decompressing needs
  about 7x (about 110 MB; 20x in pure Python). Use a smaller `block_size` on machines
  with little memory.
- The `.paw` format v1 is frozen: files written now will decode with every later version.
