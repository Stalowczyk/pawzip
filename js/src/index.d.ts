/**
 * How a block is coded. Each block of a .paw file has its own mode, and the encoder picks the
 * one that gives the smallest block.
 */
export type Mode =
  "stored" | "map" | "bwt-mtf-map" | "delta-map" | "delta-bwt-mtf-map" | "bwt-map" | "delta-bwt-map" | "x86-bwt-map";

/** Bytes: a Uint8Array (a Node.js Buffer is one), an ArrayBuffer, or another view of one. */
export type Bytes = ArrayBuffer | SharedArrayBuffer | ArrayBufferView;

export interface CompressOptions {
  /**
   * Original bytes per block: 16 MiB by default, at most 2^32 − 1. Blocks are compressed
   * independently: larger ones usually compress a little better and need more memory.
   */
  blockSize?: number;
  /**
   * The modes to try on each block, keeping the smallest result. By default a sample of each
   * large block decides which modes are worth trying, and data that's already compressed is
   * stored as it is.
   */
  modes?: readonly Mode[] | null;
  /**
   * The original file's extension, stored so the file can get its name back: "png" or
   * ".png", up to 16 ASCII letters, digits, - or _.
   */
  extension?: string | null;
  /**
   * Try every mode even on blocks whose sample shows they're already compressed (such as
   * JPEG, ZIP or video). Slower, and it rarely helps.
   */
  thorough?: boolean;
}

export interface DecompressOptions {
  /**
   * Refuse data that would decompress to more than this many bytes, with a SizeLimitError.
   * No limit by default.
   */
  maxSize?: number | null;
  /**
   * Refuse blocks that hold more than this many bytes, with a SizeLimitError: 64 MiB by
   * default. This bounds the memory used per block; a block whose compressed data is more
   * than twice this, plus 1 KiB, is refused too. `null` means no limit: only for files you
   * trust.
   */
  maxBlockSize?: number | null;
}

export interface ProgressOptions {
  /**
   * Called after each block with the bytes done so far (of the input when compressing, of
   * the .paw stream when decompressing), and at the end with the total.
   */
  onProgress?: ((done: number) => void) | null;
}

/** Facts about a .paw file, read from its headers without decompressing it. */
export interface Info {
  /** The size of the data after decompressing, in bytes. */
  originalSize: number;
  /** The size of the .paw data, in bytes. */
  compressedSize: number;
  /** The block size the file was written with: no block holds more original bytes. */
  blockSize: number;
  /** The mode of each block, in order (none for empty data). */
  modes: Mode[];
  /** The original file's extension, without the dot, or null if none was stored. */
  extension: string | null;
}

/**
 * Compress data into a complete .paw file.
 *
 * @throws {RangeError} An option is out of range, such as an extension with a space in it.
 * @throws {TypeError} The data or an option has the wrong type.
 */
export function compress(data: Bytes, options?: CompressOptions): Uint8Array<ArrayBuffer>;

/**
 * Decompress a complete .paw file. Every block's CRC-32 is checked. For data from untrusted
 * sources, the default options already cap the block size; set `maxSize` as well to cap the
 * total output.
 *
 * @throws {FormatError} Not a valid .paw file, or a damaged one.
 * @throws {SizeLimitError} The data is larger than `maxSize` or `maxBlockSize` allow.
 */
export function decompress(data: Bytes, options?: DecompressOptions): Uint8Array<ArrayBuffer>;

/**
 * Describe a .paw file from its headers, without decompressing it. The block bodies and their
 * checksums are not checked.
 *
 * @throws {FormatError} Not a valid .paw file.
 */
export function info(data: Bytes): Info;

/**
 * Compresses a stream of bytes into a .paw stream, one block at a time, so memory depends on
 * the block size and the size of the chunks written. Gives the same bytes as `compress`. Like
 * the platform's CompressionStream, it's a pair of streams for `pipeThrough`:
 *
 * ```js
 * const paw = file.stream().pipeThrough(new CompressStream({ extension: "txt" }));
 * ```
 */
export class CompressStream {
  constructor(options?: CompressOptions & ProgressOptions);
  /** The .paw stream, compressed as it's read. */
  readonly readable: ReadableStream<Uint8Array<ArrayBuffer>>;
  /** Where the data to compress is written. */
  readonly writable: WritableStream<Bytes>;
}

/**
 * Decompresses a .paw stream one block at a time, decoding a block only when the reader asks
 * for more, so memory depends on the block size and the size of the chunks written, however
 * much the data expands. The stream errors with a FormatError or a SizeLimitError where
 * `decompress` would throw one, after giving the blocks before it. Errors show on the reading
 * side: since the data is decoded as it's read, a write can be accepted before the data it
 * holds turns out bad.
 */
export class DecompressStream {
  constructor(options?: DecompressOptions & ProgressOptions);
  /** The original data, decompressed as it's read. */
  readonly readable: ReadableStream<Uint8Array<ArrayBuffer>>;
  /** Where the .paw data is written. */
  readonly writable: WritableStream<Bytes>;
}

/** The base class of the errors for data that can't be decompressed: catch it for any of them. */
export class PawError extends Error {}

/**
 * Not a valid .paw file: wrong magic, unsupported version, damaged or truncated data, or a
 * checksum mismatch.
 */
export class FormatError extends PawError {}

/**
 * The data would decompress to more than `maxSize` bytes, or holds a block larger than
 * `maxBlockSize` (or with compressed data far larger than that).
 */
export class SizeLimitError extends PawError {}

/** The block size `compress` uses unless told otherwise: 16 MiB. */
export const DEFAULT_BLOCK_SIZE: number;

/** The default `maxBlockSize` when decompressing: 64 MiB. */
export const DEFAULT_MAX_BLOCK_SIZE: number;
