# pawzip

[![PyPI](https://img.shields.io/pypi/v/pawzip)](https://pypi.org/project/pawzip/)
[![crates.io](https://img.shields.io/crates/v/pawzip)](https://crates.io/crates/pawzip)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A lossless compression algorithm that records where each byte value appears and describes
those positions with patterns.

## Install

**Python**: the library and the `paw` command

```sh
pip install pawzip
```

**Rust**: the library

```sh
cargo add pawzip
```

**Command line without Python**: the same `paw` command, on its own

```sh
cargo install pawzip-cli
```

## Use

**Python**

```python
import paw

data = b"hello world\n" * 1000
packed = paw.compress(data)
assert paw.decompress(packed) == data
```

**Rust**

```rust
let data = b"hello world\n".repeat(1000);
let packed = pawzip::compress(&data, &Default::default())?;
assert_eq!(pawzip::decompress(&packed, &Default::default())?, data);
```

**Command line**

```sh
paw compress notes.txt          # -> notes.paw (remembers .txt)
paw decompress notes.paw        # -> notes.txt
```

## How it compares

Each file of the [Silesia corpus](https://sun.aei.polsl.pl//~sdeor/index.php?page=silesia),
a standard compression benchmark, compressed on its own:

<img src="docs/images/benchmark.svg" width="100%" alt="paw compared with gzip -9, bzip2 -9 and xz -9, file by file: smaller than gzip on all 12 files, than bzip2 on 11 and than xz on 9. Against xz: reymont -19.4%, nci -18.6%, mr -17.8%, dickens -15.9%, webster -13.7%, osdb -13.1%, x-ray -13.0%, ooffice -2.7%, xml -1.6%, sao +9.5%, samba +19.5%, mozilla +31.9%.">

[All the numbers, including speed](docs/how-it-works.md#benchmark)

## Learn more

- [How it works](docs/how-it-works.md)
- [Python API and command line](python/README.md)
- [Rust API](https://docs.rs/pawzip)
- [The .paw format](spec/FORMAT.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT, see [LICENSE](LICENSE). Changes are listed in [CHANGELOG.md](CHANGELOG.md).
