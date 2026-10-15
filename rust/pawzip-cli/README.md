# pawzip-cli

The `paw` command for [pawzip](https://crates.io/crates/pawzip), a lossless compressor with
its own file format, `.paw`. It compresses, decompresses, tests and describes `.paw` files.
It is the same command as the one in the [Python package](https://pypi.org/project/pawzip/),
with the same options, messages and files, but it doesn't need Python.

## Install

```text
cargo install pawzip-cli
```

## Usage

```text
paw compress notes.txt          # -> notes.paw (remembers .txt)
paw decompress notes.paw        # -> notes.txt
paw test notes.paw              # checks that it decompresses correctly
paw info notes.paw              # sizes, blocks and modes, from the headers
```

Every command takes several files, or `-` for stdin. Status messages go to stderr, and the
exit status is 0 on success, 1 if any file failed, 2 on usage errors and 130 if interrupted.
A file that fails, or a run that is interrupted, leaves no partial output behind.

The main options (sizes can be written like `64K`, `4M` or `1.5G`):

- `-o FILE` writes to FILE and `-c` to stdout; `-f` overwrites existing files; `-q` hides the
  status messages.
- `-T N` codes N blocks at once, and `-T auto` as many as fit the CPU and the free memory.
- `-B SIZE` sets the block size (default 16M). Compressing needs up to about 100 times the
  block size in memory for each block coded at once.
- `-m MODE` tries only that mode (repeatable), and `--thorough` tries every mode even on
  data that is already compressed.
- When decompressing or testing, `--max-size SIZE` and `--max-block-size SIZE` (default
  64M) refuse files that would decompress to more, before anything large is allocated.

`paw COMMAND --help` lists everything. Installing both this and the Python package is fine:
the two `paw` commands behave the same.

## License

MIT.
