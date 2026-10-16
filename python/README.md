# pawzip

A lossless compression algorithm that records where each byte value appears and describes
those positions with patterns. It writes `.paw` files.

On the Silesia benchmark it compresses better than bzip2 and nearly as well as xz, but it is
slower than both. It does best on structured and repetitive data, and stores data it can't
compress, such as noise or files that are already compressed, as it is. The `.paw` format is
frozen, so files written today will decode with every later version.

```sh
pip install pawzip
```

Requires Python 3.10+ and NumPy. The wheels include the compressor's core, compiled from
Rust; installing from source needs [Rust](https://rustup.rs). The package is fully
type-annotated.

## Compress and decompress

```python
import paw

data = b"hello world\n" * 1000
packed = paw.compress(data)
assert paw.decompress(packed) == data
```

`compress` tries the modes that suit the data and keeps the smallest result. Data that is
already compressed (JPEG, ZIP, video) is recognised from a sample and stored as it is.
`modes=[paw.Mode.MAP]` tries only the modes you list, which is faster, and `thorough=True`
tries every mode even on already-compressed data: many times slower, for a percent or so.

## Files and streams

```python
import paw

paw.compress_file("notes.txt")  # writes notes.paw and remembers ".txt"
paw.decompress_file("notes.paw", "copy.txt")  # without a destination: notes.txt

with paw.open("log.paw", "wt", encoding="utf-8", extension="txt") as f:  # like gzip.open
    f.write("hello\n")
```

- Only the extension is stored, never the name, so a renamed `abc.paw` made from `photo.png`
  decompresses to `abc.png`. With `compress` and `compress_stream`, pass `extension="png"`
  yourself.
- Data is processed in blocks (16 MiB by default, `block_size=`), so files of any size use
  bounded memory. `compress_file`, `decompress_file`, `paw.open`, `compress_stream` and
  `decompress_stream` all stream.
- `compress_file` and `decompress_file` never overwrite a file unless `overwrite=True`, and
  never leave a partial file behind.
- The file and stream functions take a `progress` callback, called after each block (and
  once at the end) with the number of input bytes done so far.

## Untrusted input

```python
import paw

packed = paw.compress(b"data from somewhere\n" * 1000)
data = paw.decompress(packed, max_size=100 * 1024 * 1024)  # refuse more than 100 MiB
```

Every block is checked with a CRC-32. A small file can claim an enormous size, so set
`max_size` for input you don't trust: anything bigger is refused before memory is allocated.
Blocks larger than `max_block_size` (64 MiB by default) are refused the same way; files
written with the default block size are never affected.

## File information

```python
import paw

meta = paw.info(paw.compress(b"hello world\n" * 1000))  # a .paw path works too
print(meta.original_size, meta.compressed_size, meta.ratio, meta.mode_summary)
```

`info` reads only the headers, so it's fast even for large files.

## Parallel work

`workers=4` compresses or decompresses four blocks at a time, and `workers="auto"` uses as
many CPU cores as fit into half of the free memory (one where free memory can't be measured,
currently macOS). Only data with several blocks gains. Workers are threads; without the Rust
core they are processes, which need the usual `if __name__ == "__main__":` guard on Windows
and macOS.

## Errors

`paw.FormatError` (not a `.paw` file, or a damaged one) and `paw.SizeLimitError` (over
`max_size` or `max_block_size`) both derive from `paw.PawError`, which is a `ValueError`.

## Command line

```sh
paw compress notes.txt                 # -> notes.paw
paw decompress notes.paw               # -> notes.txt (also after renaming it)
paw test archive.paw                   # check it decompresses correctly
paw info archive.paw                   # sizes, blocks and modes
cat data.bin | paw compress - > data.bin.paw
paw decompress --max-size 500M -c big.paw > big
paw compress -T auto -B 8M huge.iso    # parallel, 8 MiB blocks
paw compress --thorough photo.jpg      # squeeze already-compressed files too (slow)
```

On a terminal, a progress line shows how far each file has got (`-q` hides it). Run
`paw COMMAND --help` for all options. The exit status is 0 on success, 1 if any file failed,
2 on usage errors and 130 if interrupted.

## Speed and memory

- Compression runs at about 1.7 MB/s on one core (Silesia benchmark), close to xz -9, and at
  about 3.5 MB/s with four cores, since the modes are tried in parallel. Decompression runs
  at about 18 MB/s, a little slower than bzip2 and about 3 times slower than xz.
- Compressing needs up to about 100 times the block size in memory per worker (about 1.6 GB
  for a 16 MiB block); decompressing needs about 7 times. Use a smaller `block_size` on
  machines with little memory.
- `PAW_PURE=1` switches to the pure-Python reference implementation: the same files, about
  50 times slower, and decompressing needs about 20 times the block size.

## Learn more

- [How it works](https://github.com/Stalowczyk/pawzip/blob/main/docs/how-it-works.md)
- [The .paw format](https://github.com/Stalowczyk/pawzip/blob/main/spec/FORMAT.md)
- [Rust library](https://crates.io/crates/pawzip)
- [Source code](https://github.com/Stalowczyk/pawzip)
