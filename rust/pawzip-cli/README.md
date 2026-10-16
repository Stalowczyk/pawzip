# pawzip-cli

The `paw` command for [pawzip](https://crates.io/crates/pawzip), a lossless compression
algorithm that records where each byte value appears and describes those positions with
patterns. It's the same command as in the [Python package](https://pypi.org/project/pawzip/),
without needing Python.

## Install

```sh
cargo install pawzip-cli
```

## Usage

```sh
paw compress notes.txt          # -> notes.paw (remembers .txt)
paw decompress notes.paw        # -> notes.txt
paw test notes.paw              # check that it decompresses correctly
paw info notes.paw              # sizes, blocks and modes, from the headers
```

Every command takes several files, or `-` for stdin. A file that fails, or a run that is
interrupted, leaves no partial output behind. The exit status is 0 on success, 1 if any file
failed, 2 on usage errors and 130 if interrupted.

## Options

| Option | Effect |
|--------|--------|
| `-o FILE` | write to FILE |
| `-c` | write to stdout |
| `-f` | overwrite existing files |
| `-q` | no status messages |
| `-T N` | code N blocks at once; `-T auto` fits the CPU and the free memory |
| `-B SIZE` | block size, 16M by default; compressing needs up to about 100 times this in memory per block |
| `-m MODE` | try only this mode (repeatable) |
| `--thorough` | try every mode, even on data that is already compressed |
| `--max-size SIZE` | when decompressing, refuse output larger than SIZE |
| `--max-block-size SIZE` | when decompressing, refuse blocks larger than SIZE (64M by default) |

Sizes can be written like `64K`, `4M` or `1.5G`. `paw COMMAND --help` lists everything.

## License

MIT.
