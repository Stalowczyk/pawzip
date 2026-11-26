// The declarations in use, type-checked by `npm run check` (never run): what should compile
// does, and what shouldn't is marked with @ts-expect-error.
import {
  CompressStream,
  DEFAULT_BLOCK_SIZE,
  DEFAULT_MAX_BLOCK_SIZE,
  DecompressStream,
  FormatError,
  PawError,
  SizeLimitError,
  compress,
  decompress,
  info,
  type Bytes,
  type Info,
  type Mode,
} from "pawzip";

const data = new TextEncoder().encode("hello world\n".repeat(1000));
const packed: Uint8Array<ArrayBuffer> = compress(data, {
  blockSize: DEFAULT_BLOCK_SIZE,
  modes: ["bwt-map", "map"],
  extension: "txt",
  thorough: false,
});
const back: Uint8Array<ArrayBuffer> = decompress(packed, { maxSize: 1 << 20, maxBlockSize: DEFAULT_MAX_BLOCK_SIZE });
decompress(packed.buffer, { maxSize: null, maxBlockSize: null });
compress(new DataView(back.buffer));

// The results go straight into a Blob or a Response, without a cast.
new Blob([packed, back]);
new Response(packed);

const facts: Info = info(packed);
const sizes: number = facts.originalSize + facts.compressedSize + facts.blockSize;
const first: Mode | undefined = facts.modes[0];
const extension: string | null = facts.extension;

const roundTrip: ReadableStream<Uint8Array<ArrayBuffer>> = new Blob([data])
  .stream()
  .pipeThrough(new CompressStream({ extension: "txt", onProgress: (done: number) => console.log(done) }))
  .pipeThrough(new DecompressStream({ maxSize: data.length }));

try {
  decompress(packed);
} catch (e) {
  if (e instanceof FormatError || e instanceof SizeLimitError) {
    const base: PawError = e;
    const message: string = base.message;
    void message;
  }
}

const input: Bytes = new Uint16Array(4);
void [sizes, first, extension, roundTrip, input];

// @ts-expect-error: not a mode
compress(data, { modes: ["gzip"] });
// @ts-expect-error: a string isn't bytes
compress("hello");
// @ts-expect-error: no such option
compress(data, { level: 9 });
// @ts-expect-error: streams take their options, not a format name
new CompressStream("gzip");
// @ts-expect-error: maxBlockSize is a number of bytes
decompress(packed, { maxBlockSize: "64 MiB" });
