# pawzip

A lossless compression algorithm that records where each byte value appears and describes
those positions with patterns. This package is its JavaScript version: the Rust
implementation compiled to WebAssembly. It reads and writes `.paw` files, the same files as the
[Python package](https://pypi.org/project/pawzip/), the
[Rust crate](https://crates.io/crates/pawzip) and the `paw` command.

On the Silesia benchmark it compresses better than bzip2 and nearly as well as xz, but it is
slower than both. It does best on structured and repetitive data, and stores data it can't
compress, such as noise or files that are already compressed, as it is. The `.paw` format is
frozen, so files written today will decode with every later version.

```sh
npm install pawzip
```

It works in Node.js 22.12 or newer, Deno, Bun and browsers, with or without a bundler, through
`import` or `require()`. There's nothing to set up and no dependencies: the WebAssembly module
is part of the JavaScript, and it's compiled the first time pawzip is called. Types are
included, for TypeScript 5.7 or newer (5.8 for `require()`).

## Usage

```js
import { compress, decompress } from "pawzip";

const data = new TextEncoder().encode("hello world\n".repeat(1000));
const packed = compress(data); // a Uint8Array
const back = decompress(packed);
```

The functions take a `Uint8Array` (a Node.js `Buffer` is one), an `ArrayBuffer` or another view
of one, and return a `Uint8Array`. `info` describes a `.paw` file from its headers, without
decompressing it:

```js
import { info } from "pawzip";

info(packed);
// { originalSize: 12000, compressedSize: 56, blockSize: 16777216, modes: ["map"], extension: null }
```

## Streams

`CompressStream` and `DecompressStream` work like the platform's `CompressionStream`. The data
goes through one block at a time, and only as fast as it's read, so the memory they need
depends on the block size and the size of the chunks written, not on how large the data is
or how much it expands. They give the same bytes as `compress` and `decompress`.

In a browser:

```js
import { CompressStream } from "pawzip";

const paw = file.stream().pipeThrough(new CompressStream({ extension: "txt" }));
const blob = await new Response(paw).blob();
```

In Node.js, with files:

```js
import { createReadStream, createWriteStream } from "node:fs";
import { Duplex } from "node:stream";
import { pipeline } from "node:stream/promises";
import { DecompressStream } from "pawzip";

await pipeline(
  createReadStream("notes.paw"),
  Duplex.fromWeb(new DecompressStream()),
  createWriteStream("notes.txt"),
);
```

Both take `onProgress`, called after each block with the number of bytes done so far: of the
input when compressing, of the `.paw` data when decompressing.

## Options

For `compress` and `CompressStream`:

- `blockSize`: original bytes per block, 16 MiB by default. Larger blocks usually compress a
  little better and need more memory.
- `extension`: the original file's extension, such as `"txt"`, stored so that the file gets its
  name back when the `paw` command decompresses it.
- `modes`: the modes to try on each block, such as `["bwt-map"]`. By default a sample of each
  large block decides which are worth trying.
- `thorough`: try every mode even on data that looks already compressed. Slower, and it rarely
  helps.

For `decompress` and `DecompressStream`, the limits below.

## Untrusted input

Decompressing checks every block's CRC-32, and it refuses blocks larger than 64 MiB by default
(and blocks whose compressed data is far larger than that), from their headers, before
anything large is allocated. To also cap the total size of the output, set `maxSize`:

```js
import { PawError, SizeLimitError, decompress } from "pawzip";

try {
  const data = decompress(upload, { maxSize: 100_000_000 });
} catch (e) {
  if (e instanceof SizeLimitError) {
    // larger than allowed
  } else if (e instanceof PawError) {
    // a FormatError: not a .paw file, or a damaged one
  } else {
    throw e;
  }
}
```

`maxBlockSize: null` lifts the block limit, for files you trust that were written with larger
blocks.

## Errors

- `FormatError`: not a valid `.paw` file: wrong magic, unsupported version, damaged or
  truncated data, or a checksum mismatch.
- `SizeLimitError`: larger than `maxSize` or `maxBlockSize` allow.

Both are `PawError`s. Wrong arguments throw a `TypeError` or a `RangeError`.

## Speed and memory

Everything runs on the calling thread, so in a browser, call pawzip from a worker to keep the
page responsive. On a desktop PC it compresses at about 2 MB/s (0.8 to 5 MB/s, depending on the
data) and decompresses at 10 to 30 MB/s. That's close to the native library on one core; with
more cores the native library compresses faster, because it tries a block's modes in parallel
threads.

Besides the data passed in and out, compressing needs up to about 30 times the block size in
memory (430 MiB with 16 MiB blocks), and decompressing about 10 times. With 4 MiB blocks,
compressing needs about a third as much, and files are about 3% bigger. WebAssembly memory
grows as needed and isn't given back. A call that runs out of memory throws a `WebAssembly.RuntimeError`, after which
pawzip can't be relied on in that thread: in a worker, start a new one.

In browsers, a Content Security Policy that restricts scripts (`script-src` or `default-src`)
has to allow WebAssembly, with `'wasm-unsafe-eval'`, for pawzip to work; without that,
importing pawzip still works, and its calls throw.
Cloudflare Workers aren't supported: they only run WebAssembly modules uploaded with the
Worker, not ones compiled at run time.

## Learn more

- [How it works](https://github.com/Stalowczyk/pawzip/blob/main/docs/how-it-works.md)
- [The .paw format](https://github.com/Stalowczyk/pawzip/blob/main/spec/FORMAT.md)
- [Python package](https://pypi.org/project/pawzip/)
- [Rust crate](https://crates.io/crates/pawzip)
- [Source code](https://github.com/Stalowczyk/pawzip)

## License

MIT.
