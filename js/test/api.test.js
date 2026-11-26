// The functions: inputs, options, limits for untrusted data, and errors.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import {
  DEFAULT_BLOCK_SIZE,
  DEFAULT_MAX_BLOCK_SIZE,
  FormatError,
  PawError,
  SizeLimitError,
  compress,
  decompress,
  info,
} from "pawzip";
import { MODES, assertBytes, failure, varint } from "./helpers.js";

const text = new TextEncoder().encode("the quick brown fox jumps over the lazy dog\n".repeat(500));

test("round trips", () => {
  for (const data of [new Uint8Array(0), Uint8Array.of(0), text, random(70_000)]) {
    const packed = compress(data);
    assertBytes(decompress(packed), data, `${data.length} bytes`);
  }
  assert.ok(compress(text).length < text.length / 10, "text gets much smaller");
});

test("any view of bytes, and an ArrayBuffer, can be given", () => {
  const packed = compress(text);
  const buffer = Buffer.from(text);
  const offset = new Uint8Array(text.length + 10);
  offset.set(text, 5);
  for (const input of [
    buffer,
    text.buffer,
    new DataView(text.buffer),
    offset.subarray(5, 5 + text.length),
    new Uint16Array(text.buffer, 0, text.length / 2),
  ]) {
    assertBytes(compress(input), packed, input.constructor.name);
  }
  const wide = new Uint16Array(text.buffer, 0, text.length / 2);
  assertBytes(decompress(compress(wide)), text, "the bytes of a Uint16Array");
  assertBytes(decompress(packed.buffer), text, "decompress from an ArrayBuffer");
  for (const bad of ["text", 42, null, undefined, [1, 2, 3], {}]) {
    assert.throws(() => compress(bad), TypeError, String(bad));
    assert.throws(() => decompress(bad), TypeError, String(bad));
    assert.throws(() => info(bad), TypeError, String(bad));
  }
});

test("the results are Uint8Arrays of their own", () => {
  const packed = compress(text);
  const back = decompress(packed);
  for (const out of [packed, back]) {
    assert.equal(Object.getPrototypeOf(out), Uint8Array.prototype);
    assert.equal(out.byteOffset, 0);
    assert.equal(out.buffer.byteLength, out.length);
  }
});

test("info reads the headers", () => {
  const packed = compress(text, { blockSize: 5000, extension: ".TXT" });
  const { modes, ...sizes } = info(packed);
  assert.deepEqual(sizes, {
    originalSize: text.length,
    compressedSize: packed.length,
    blockSize: 5000,
    extension: "TXT",
  });
  assert.equal(modes.length, Math.ceil(text.length / 5000));
  assert.ok(
    modes.every((mode) => MODES.includes(mode)),
    modes.join(),
  );
  assert.deepEqual(
    info(compress(text, { blockSize: 5000, modes: ["delta-bwt-map"] })).modes,
    Array(5).fill("delta-bwt-map"),
  );
  assert.deepEqual(info(compress(new Uint8Array(0))), {
    originalSize: 0,
    compressedSize: compress(new Uint8Array(0)).length,
    blockSize: DEFAULT_BLOCK_SIZE,
    modes: [],
    extension: null,
  });
  assert.throws(() => info(new Uint8Array([1, 2, 3])), FormatError);
});

test("compress options", () => {
  assert.deepEqual(info(compress(text, { modes: ["stored"] })).modes, ["stored"]);
  assert.equal(info(compress(text, { modes: ["bwt-map", "map"] })).modes.length, 1);
  assert.deepEqual(info(compress(random(300_000))).modes, ["stored"]);
  const thorough = compress(text, { thorough: true });
  assertBytes(decompress(thorough), text, "thorough");
  assert.equal(info(compress(text, { extension: "tar-gz_2" })).extension, "tar-gz_2");
  assert.equal(info(compress(text, { extension: "" })).extension, null);
  assert.equal(info(compress(text, { extension: null, modes: null })).extension, null);
  assertBytes(compress(text, {}), compress(text), "empty options");
  assertBytes(compress(text, { blockSize: DEFAULT_BLOCK_SIZE }), compress(text), "the default block size");
});

test("bad compress options are refused before any work", () => {
  const cases = [
    [{ blockSize: 0 }, RangeError],
    [{ blockSize: 2 ** 32 }, RangeError],
    [{ blockSize: 1.5 }, RangeError],
    [{ blockSize: "16" }, RangeError],
    [{ modes: [] }, TypeError],
    [{ modes: "map" }, TypeError],
    [{ modes: ["gzip"] }, RangeError],
    [{ modes: [1] }, RangeError],
    [{ extension: 7 }, TypeError],
    [{ extension: "no spaces" }, RangeError],
    [{ extension: "a".repeat(17) }, RangeError],
    [{ extension: "é" }, RangeError],
    [{ thorough: "yes" }, TypeError],
  ];
  for (const [options, type] of cases) {
    assert.throws(() => compress(text, options), type, JSON.stringify(options));
  }
  assert.throws(() => compress(text, "fast"), TypeError);
});

test("size limits refuse data before decompressing it", async () => {
  const packed = compress(text, { blockSize: 4096 });
  assertBytes(decompress(packed, { maxSize: text.length }), text, "exactly the limit");
  assert.throws(() => decompress(packed, { maxSize: text.length - 1 }), SizeLimitError);
  assert.throws(() => decompress(packed, { maxBlockSize: 4095 }), SizeLimitError);
  assertBytes(decompress(packed, { maxBlockSize: 4096, maxSize: null }), text, "maxBlockSize");

  // A header, then the start of a block claiming more than 64 MiB, and nothing else: the
  // default limit refuses it from the block header, before the body is needed.
  const n = DEFAULT_MAX_BLOCK_SIZE + 1;
  const claim = Uint8Array.from([0x50, 0x41, 0x57, 1, ...varint(n), 0, 0, ...varint(n)]);
  const error = await failure(() => decompress(claim));
  assert.ok(error instanceof SizeLimitError, String(error));
  assert.match(error.message, /maxBlockSize=67,108,864/);
  assert.throws(() => decompress(claim, { maxBlockSize: null }), FormatError, "without a limit: truncated");

  for (const bad of [{ maxSize: -1 }, { maxSize: 1.5 }, { maxBlockSize: "1" }, { maxSize: 2 ** 53 }]) {
    assert.throws(() => decompress(packed, bad), RangeError, JSON.stringify(bad));
  }
});

test("importing doesn't compile WebAssembly: only calls do", () => {
  // As on a page whose Content Security Policy forbids WebAssembly: importing still works, so
  // an app that uses pawzip for one feature still starts, and the call fails.
  const script = `
    globalThis.WebAssembly = undefined;
    const paw = await import("pawzip");
    try {
      paw.compress(new Uint8Array(1));
      console.log("compressed");
    } catch (e) {
      console.log("the call failed:", e.name);
    }`;
  const out = execFileSync(process.execPath, ["--input-type=module", "-e", script], {
    cwd: new URL("..", import.meta.url),
    encoding: "utf8",
  });
  assert.equal(out.trim(), "the call failed: TypeError");
});

test("errors are PawErrors with a name and a message", async () => {
  const damaged = compress(text);
  damaged[damaged.length - 20] ^= 0xff;
  for (const [data, type] of [
    [new TextEncoder().encode("not paw"), FormatError],
    [damaged, FormatError],
    [compress(text).subarray(0, 30), FormatError],
  ]) {
    const error = await failure(() => decompress(data));
    assert.ok(error instanceof type && error instanceof PawError && error instanceof Error);
    assert.equal(error.name, type.name);
    assert.ok(error.message.length > 0);
    assert.ok(String(error).startsWith(`${type.name}: `));
  }
  assert.equal(new SizeLimitError("x").name, "SizeLimitError");
  assert.ok(!(new FormatError("x") instanceof SizeLimitError));
});

/** Random bytes, which don't compress (getRandomValues gives at most 64 KiB at a time). */
function random(length) {
  const out = new Uint8Array(length);
  for (let i = 0; i < length; i += 65536) crypto.getRandomValues(out.subarray(i, i + 65536));
  return out;
}
