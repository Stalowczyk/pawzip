// pawzip for JavaScript: the Rust core compiled to WebAssembly, behind the functions of the
// Python package and the Rust crate. Everything runs synchronously on the calling thread, so
// for large data in a browser, call it from a worker.

import {
  Compressor,
  Decompressor,
  compress as compressWasm,
  decompress as decompressWasm,
  info as infoWasm,
  initSync,
} from "./pawzip_wasm.js";
import wasmBase64 from "./pawzip_wasm.base64.js";

/** The block size `compress` uses unless told otherwise: 16 MiB. */
export const DEFAULT_BLOCK_SIZE = 16 * 1024 * 1024;
/** The default `maxBlockSize` when decompressing: 64 MiB. */
export const DEFAULT_MAX_BLOCK_SIZE = 64 * 1024 * 1024;
const FORMAT_MAX_BLOCK_SIZE = 2 ** 32 - 1;
/** The modes by the byte that stands for each in a block header. */
const MODES = [
  "stored",
  "map",
  "bwt-mtf-map",
  "delta-map",
  "delta-bwt-mtf-map",
  "bwt-map",
  "delta-bwt-map",
  "x86-bwt-map",
];
/** Input goes to the WebAssembly side at most this much at a time, so the memory it uses
 *  doesn't grow with the size of the chunks a stream is given. */
const PIECE = 1 << 20;

/** The base class of the errors for data that can't be decompressed: catch it for any of them. */
export class PawError extends Error {}
PawError.prototype.name = "PawError";

/** Not a valid .paw file: wrong magic, unsupported version, damaged or truncated data, or a
 *  checksum mismatch. */
export class FormatError extends PawError {}
FormatError.prototype.name = "FormatError";

/** The data would decompress to more than `maxSize` bytes, or holds a block larger than
 *  `maxBlockSize` (or with compressed data far larger than that). */
export class SizeLimitError extends PawError {}
SizeLimitError.prototype.name = "SizeLimitError";

/** Compress data into a complete .paw file. */
export function compress(data, options) {
  const args = compressArgs(options);
  ready();
  return call(() => compressWasm(bytes(data), ...args));
}

/** Decompress a complete .paw file. Every block's CRC-32 is checked. */
export function decompress(data, options) {
  const args = decompressArgs(options);
  ready();
  return call(() => decompressWasm(bytes(data), ...args));
}

/** Describe a .paw file from its headers, without decompressing it. */
export function info(data) {
  ready();
  return call(() => infoWasm(bytes(data)));
}

/**
 * Compresses a stream of bytes into a .paw stream, one block at a time. A pair of streams like
 * the platform's CompressionStream: write to `writable` and read from `readable`, or use it with
 * `pipeThrough`. Input is compressed only as the output is read.
 */
export class CompressStream {
  constructor(options) {
    const args = compressArgs(options);
    const report = reporter(options);
    ready();
    let compressor = call(() => new Compressor(...args));
    // No more than a block at a time, so that each piece completes at most one block.
    const pieceSize = Math.min(PIECE, args[0]);
    const input = new TransformStream();
    const reader = input.readable.getReader();
    let rest = new Uint8Array(0); // of the chunk being compressed
    let total = 0;
    this.writable = input.writable;
    this.readable = new ReadableStream(
      {
        // When the reader wants more: compress input until there's output, taking no more
        // input than that needs.
        async pull(controller) {
          try {
            for (;;) {
              if (rest.length > 0) {
                const piece = rest.subarray(0, pieceSize);
                rest = rest.subarray(piece.length);
                const out = compressor.push(piece);
                if (out.length > 0) {
                  controller.enqueue(out);
                  if (compressor.done > 0) report(compressor.done);
                  return;
                }
                continue;
              }
              const { value, done } = await reader.read();
              if (compressor === null) return; // cancelled while waiting: it's freed
              if (done) {
                const finishing = compressor;
                compressor = null; // finish() frees it
                controller.enqueue(finishing.finish());
                report(total);
                controller.close();
                return;
              }
              rest = copy(value);
              total += rest.length;
            }
          } catch (e) {
            const engine = compressor;
            compressor = null;
            throw fail(e, engine, reader);
          }
        },
        cancel(reason) {
          const engine = compressor;
          compressor = null;
          release(engine);
          return reader.cancel(reason);
        },
      },
      { highWaterMark: 0 },
    );
  }
}

/**
 * Decompresses a .paw stream, one block at a time: a block is decoded only when the reader
 * asks for more, so memory depends on the block size and the size of the chunks written,
 * however much the data expands. A pair of streams like the platform's DecompressionStream.
 */
export class DecompressStream {
  constructor(options) {
    const args = decompressArgs(options);
    const report = reporter(options);
    ready();
    let decompressor = new Decompressor(...args);
    const input = new TransformStream();
    const reader = input.readable.getReader();
    let rest = new Uint8Array(0); // of the chunk being taken in
    this.writable = input.writable;
    this.readable = new ReadableStream(
      {
        // When the reader wants more: the next block, taking in input until it's complete.
        async pull(controller) {
          try {
            for (;;) {
              const block = decompressor.next_block();
              if (block !== undefined) {
                controller.enqueue(block);
                report(decompressor.consumed);
                return;
              }
              if (rest.length > 0) {
                const piece = rest.subarray(0, PIECE);
                rest = rest.subarray(piece.length);
                decompressor.push(piece);
                continue;
              }
              const { value, done } = await reader.read();
              if (decompressor === null) return; // cancelled while waiting: it's freed
              if (done) {
                const consumed = decompressor.consumed;
                const finishing = decompressor;
                decompressor = null; // finish() frees it
                finishing.finish();
                report(consumed);
                controller.close();
                return;
              }
              rest = copy(value);
            }
          } catch (e) {
            const engine = decompressor;
            decompressor = null;
            throw fail(e, engine, reader);
          }
        },
        cancel(reason) {
          const engine = decompressor;
          decompressor = null;
          release(engine);
          return reader.cancel(reason);
        },
      },
      { highWaterMark: 0 },
    );
  }
}

let compiled = false;

/** Compile the WebAssembly module, the first time it's needed. It's inlined as base64, so the
 *  package is plain JavaScript that loads the same way in every runtime and bundler, with no
 *  file to fetch or locate; and it's small (about 250 KB) so it compiles synchronously, even on
 *  a browser's main thread. Compiling on first use rather than on import means a page that
 *  can't run WebAssembly (such as one whose Content Security Policy forbids it) only fails
 *  where it calls pawzip. */
function ready() {
  if (!compiled) {
    initSync({ module: fromBase64(wasmBase64) });
    compiled = true;
  }
}

function compressArgs(options) {
  const { blockSize = DEFAULT_BLOCK_SIZE, modes, extension, thorough = false } = object(options);
  if (!Number.isInteger(blockSize) || blockSize < 1 || blockSize > FORMAT_MAX_BLOCK_SIZE) {
    throw new RangeError(`blockSize must be a whole number of bytes from 1 to ${FORMAT_MAX_BLOCK_SIZE}`);
  }
  let modeBytes;
  if (modes != null) {
    if (!Array.isArray(modes) || modes.length === 0) {
      throw new TypeError('modes must be a non-empty array of mode names, such as ["bwt-map"]');
    }
    modeBytes = Uint8Array.from(modes, (mode) => {
      const byte = MODES.indexOf(mode);
      if (byte < 0) throw new RangeError(`unknown mode ${JSON.stringify(mode)}: the modes are ${MODES.join(", ")}`);
      return byte;
    });
  }
  if (extension != null && typeof extension !== "string") {
    throw new TypeError('extension must be a string, such as "txt"');
  }
  if (typeof thorough !== "boolean") throw new TypeError("thorough must be true or false");
  return [blockSize, modeBytes, extension ?? undefined, thorough];
}

function decompressArgs(options) {
  const { maxSize = null, maxBlockSize = DEFAULT_MAX_BLOCK_SIZE } = object(options);
  return [limit(maxSize, "maxSize"), limit(maxBlockSize, "maxBlockSize")];
}

/** A size limit for the WebAssembly side, where `undefined` means no limit. */
function limit(value, name) {
  if (value === null) return undefined;
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new RangeError(`${name} must be a whole number of bytes, or null for no limit`);
  }
  return value;
}

function object(options) {
  if (options == null) return {};
  if (typeof options !== "object") throw new TypeError("options must be an object");
  return options;
}

/** `onProgress` called with each new count of bytes done. */
function reporter(options) {
  const { onProgress } = object(options);
  if (onProgress == null) return () => {};
  if (typeof onProgress !== "function") throw new TypeError("onProgress must be a function");
  let last = -1;
  return (done) => {
    if (done !== last) {
      last = done;
      onProgress(done);
    }
  };
}

/** The bytes of a Uint8Array (such as a Node.js Buffer), an ArrayBuffer or another view. */
function bytes(data) {
  if (data instanceof Uint8Array) return data;
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  const tag = Object.prototype.toString.call(data);
  if (tag === "[object ArrayBuffer]" || tag === "[object SharedArrayBuffer]") return new Uint8Array(data);
  throw new TypeError(`expected a Uint8Array, an ArrayBuffer or another view of one, not ${tag.slice(8, -1)}`);
}

/** A copy of a chunk written to a stream: a writer may reuse or hand off its buffer as soon as
 *  its write is done, while the stream takes the chunk in a piece at a time. */
function copy(chunk) {
  return bytes(chunk).slice();
}

function call(f) {
  try {
    return f();
  } catch (e) {
    throw convert(e);
  }
}

/** The error to throw for one from the WebAssembly side, whose name tells its kind (see
 *  rust/pawzip-wasm). Others, such as a WebAssembly.RuntimeError when memory runs out, pass
 *  through unchanged. */
function convert(e) {
  if (e instanceof Error) {
    switch (e.name) {
      case "PawFormatError":
        return new FormatError(e.message);
      case "PawSizeLimitError":
        return new SizeLimitError(e.message);
      case "PawInvalidError":
        return new RangeError(e.message);
      case "PawError":
        return new PawError(e.message);
    }
  }
  return e;
}

/** The error a stream fails with. Its WebAssembly object is freed, and the writing side is
 *  told, so that writes waiting there fail too. */
function fail(e, engine, reader) {
  const error = convert(e);
  release(engine);
  reader.cancel(error).catch(() => {});
  return error;
}

/** Free a stream's WebAssembly object early, after an error or a cancel. */
function release(engine) {
  try {
    engine?.free();
  } catch {
    // The module stopped (a WebAssembly trap): nothing more to do.
  }
}

function fromBase64(text) {
  if (typeof Uint8Array.fromBase64 === "function") return Uint8Array.fromBase64(text);
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}
