# pawzip

A lossless compression algorithm that records where each byte value appears and describes
those positions with patterns. This crate is its Rust implementation: it reads and writes
`.paw` files, the same files as the [Python package](https://pypi.org/project/pawzip/). The
`paw` command is the [`pawzip-cli`](https://crates.io/crates/pawzip-cli) crate.

On the Silesia benchmark it compresses better than bzip2 and nearly as well as xz, but it is
slower than both. It does best on structured and repetitive data, and stores data it can't
compress, such as noise or files that are already compressed, as it is. The `.paw` format is
frozen, so files written today will decode with every later version.

```sh
cargo add pawzip
```

Requires Rust 1.87 or newer. The library has no unsafe code.

## Usage

Compress and decompress data in memory:

```rust
fn main() -> Result<(), pawzip::Error> {
    let data = b"hello world\n".repeat(1000);
    let paw = pawzip::compress(&data, &Default::default())?;
    assert_eq!(pawzip::decompress(&paw, &Default::default())?, data);
    Ok(())
}
```

Streams, such as files, are handled block by block, so memory doesn't grow with the size of
the data:

```rust,no_run
use std::fs::File;
use std::io::{BufReader, BufWriter};

fn main() -> Result<(), pawzip::Error> {
    let mut options = pawzip::CompressOptions::default();
    options.extension = Some("txt".into()); // lets the paw command restore notes.txt
    let src = BufReader::new(File::open("notes.txt").map_err(pawzip::Error::Io)?);
    let dst = BufWriter::new(File::create("notes.paw").map_err(pawzip::Error::Io)?);
    pawzip::compress_stream(src, dst, &options, &mut |done| eprintln!("{done} bytes"))?;
    Ok(())
}
```

`decompress_stream` works the same way, and `info` describes a `.paw` file from its headers
without decompressing it.

## Untrusted input

Decompressing checks every block's CRC-32, and it refuses blocks larger than 64 MiB by
default, before anything large is allocated. To also cap the total size of the output:

```rust
fn main() -> Result<(), pawzip::Error> {
    let paw = pawzip::compress(&[0; 5000], &Default::default())?;
    let mut options = pawzip::DecompressOptions::default();
    options.max_size = Some(1000);
    match pawzip::decompress(&paw, &options) {
        Err(pawzip::Error::SizeLimit(message)) => println!("refused: {message}"),
        other => panic!("expected a size limit error, got {other:?}"),
    }
    Ok(())
}
```

## Speed and memory

The data is cut into blocks of 16 MiB by default, and each block is compressed on its own:
the encoder tries several modes and keeps the smallest result. Compressing needs up to
about 100 times the block size in memory (about 1.6 GB for 16 MiB blocks), because the modes
are tried in parallel threads; decompressing needs about 7 times. Several blocks can be
compressed or decompressed at once with `workers`, at that cost per worker. Builds for
WebAssembly use no threads.

## Learn more

- [How it works](https://github.com/Stalowczyk/pawzip/blob/main/docs/how-it-works.md)
- [The .paw format](https://github.com/Stalowczyk/pawzip/blob/main/spec/FORMAT.md)
- [Python package](https://pypi.org/project/pawzip/)
- [Source code](https://github.com/Stalowczyk/pawzip)

## License

MIT.
