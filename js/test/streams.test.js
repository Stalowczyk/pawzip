// CompressStream and DecompressStream: the same bytes as the functions, block by block.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { createReadStream, createWriteStream, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Duplex } from "node:stream";
import { pipeline } from "node:stream/promises";
import { test } from "node:test";
import { CompressStream, DecompressStream, FormatError, SizeLimitError, compress, decompress, info } from "pawzip";
import { assertBytes, concat, failure, pipe, varint } from "./helpers.js";

const text = new TextEncoder().encode("one line of text, then another one\n".repeat(3000)); // 105,000 bytes

test("CompressStream writes the bytes compress writes, wherever the input is cut", async () => {
  for (const blockSize of [10_000, 35_000, 1 << 20]) {
    const options = { blockSize, extension: "log" };
    const expected = compress(text, options);
    for (const cut of [1000, 10_000, 35_000, 50_000, text.length]) {
      assertBytes(
        await pipe(text, new CompressStream(options), cut),
        expected,
        `blocks of ${blockSize}, pieces of ${cut}`,
      );
    }
  }
  assertBytes(await pipe(new Uint8Array(0), new CompressStream()), compress(new Uint8Array(0)), "nothing");
});

test("DecompressStream gives back the data, block by block", async () => {
  const packed = compress(text, { blockSize: 10_000 });
  assert.equal(info(packed).modes.length, 11);
  // One chunk per block, whether the input comes in small pieces or all at once.
  for (const pieces of [Array.from(packed, (byte) => Uint8Array.of(byte)), [packed]]) {
    const chunks = await Array.fromAsync(ReadableStream.from(pieces).pipeThrough(new DecompressStream()));
    assert.deepEqual(
      chunks.map((c) => c.length),
      [...Array(10).fill(10_000), 5000],
    );
  }
  assertBytes(await pipe(packed, new DecompressStream(), 777), text, "pieces of 777");
  assertBytes(await pipe(packed, new DecompressStream(), packed.length), text, "all at once");
});

test("progress is reported after each block and at the end", async () => {
  const seen = [];
  const packed = await pipe(text, new CompressStream({ blockSize: 30_000, onProgress: (n) => seen.push(n) }), 7000);
  assert.deepEqual(seen, [30_000, 60_000, 90_000, text.length]);

  const read = [];
  await pipe(packed, new DecompressStream({ onProgress: (n) => read.push(n) }), 10);
  assert.equal(read.length, info(packed).modes.length + 1, "after each block, then at the end");
  assert.equal(read.at(-1), packed.length);
  assert.ok(
    read.every((n, i) => i === 0 || n > read[i - 1]),
    "always more",
  );

  const empty = [];
  await pipe(new Uint8Array(0), new CompressStream({ onProgress: (n) => empty.push(n) }));
  assert.deepEqual(empty, [0], "nothing to compress: one report, at the end");
});

test("a block body far larger than maxBlockSize allows is refused before it's taken in", async () => {
  // One 1-byte block whose body is declared as 100 MB, and then 100 MB arriving: a padded body
  // like that can be valid, but the limit refuses it from the block's header.
  const header = [0x50, 0x41, 0x57, 1, 1, 0, 0, 1, 1, 0, 0, 0, 0, ...varint(100_000_000)];
  let sent = 0;
  const source = new ReadableStream({
    pull(controller) {
      const chunk = sent === 0 ? Uint8Array.from(header) : new Uint8Array(65536);
      sent += chunk.length;
      controller.enqueue(chunk);
    },
  });
  const error = await failure(() => Array.fromAsync(source.pipeThrough(new DecompressStream({ maxBlockSize: 1000 }))));
  assert.ok(error instanceof SizeLimitError, String(error));
  assert.equal(error.message, "a block's body is 100,000,000 bytes, more than maxBlockSize=1,000 allows");
  assert.ok(sent < 1 << 20, `${sent} bytes taken in`);
});

test("damaged and oversized streams error as decompress throws", async () => {
  const packed = compress(text, { blockSize: 10_000 });
  for (const [data, options] of [
    [packed.subarray(0, packed.length - 1), {}],
    [Uint8Array.of(...packed, 0), {}],
    [new TextEncoder().encode("not a paw file at all"), {}],
    [packed, { maxSize: 50_000 }],
    [packed, { maxBlockSize: 9999 }],
  ]) {
    const expected = await failure(() => decompress(data, options));
    const error = await failure(() => pipe(data, new DecompressStream(options), 4096));
    assert.ok(error instanceof (expected instanceof SizeLimitError ? SizeLimitError : FormatError), String(error));
    assert.equal(error.message, expected.message);
  }
});

test("bad options throw at once, and bad chunks error the stream", async () => {
  assert.throws(() => new CompressStream({ blockSize: 0 }), RangeError);
  assert.throws(() => new CompressStream({ extension: "a b" }), RangeError);
  assert.throws(() => new CompressStream({ onProgress: "yes" }), TypeError);
  assert.throws(() => new DecompressStream({ maxSize: -1 }), RangeError);
  for (const stream of [new CompressStream(), new DecompressStream()]) {
    const error = await failure(() => Array.fromAsync(ReadableStream.from(["text"]).pipeThrough(stream)));
    assert.ok(error instanceof TypeError, String(error));
  }
});

test("data that expands a lot comes out a block at a time, as it's read", async () => {
  // 64 blocks of zeros in a few hundred bytes: all of it arrives in the first chunk written.
  const blockSize = 1 << 20;
  const packed = compress(new Uint8Array(64 * blockSize), { blockSize, modes: ["map"] });
  assert.ok(packed.length < 4096);
  const decoded = [];
  const stream = new DecompressStream({ onProgress: (n) => decoded.push(n) });
  const writer = stream.writable.getWriter();
  void writer.write(packed);
  void writer.close();
  const reader = stream.readable.getReader();
  for (let i = 1; i <= 3; i++) {
    const { value } = await reader.read();
    assert.equal(value.length, blockSize, "one block per chunk");
    assert.equal(decoded.length, i, "nothing decoded ahead of the reader");
  }
  let rest = 0;
  for (let r = await reader.read(); !r.done; r = await reader.read()) rest++;
  assert.equal(rest, 61);
});

test("a large chunk is compressed a block at a time, as it's read", async () => {
  const blockSize = 1 << 16;
  const data = new Uint8Array(randomBytes(20 * blockSize));
  const done = [];
  const stream = new CompressStream({ blockSize, modes: ["stored"], onProgress: (n) => done.push(n) });
  const writer = stream.writable.getWriter();
  void writer.write(data);
  void writer.close();
  const reader = stream.readable.getReader();
  const chunks = [];
  for (let r = await reader.read(); !r.done; r = await reader.read()) {
    chunks.push(r.value);
    assert.ok(r.value.length <= blockSize + 64, "at most one block per chunk");
    assert.ok(done.length <= chunks.length, "nothing compressed ahead of the reader");
  }
  assertBytes(decompress(concat(chunks)), data, "the data");
  assert.deepEqual(
    done,
    Array.from({ length: 20 }, (_, i) => (i + 1) * blockSize),
  );
});

test("the blocks before a damaged one are given, then the error", async () => {
  const packed = compress(text, { blockSize: 10_000 });
  packed[packed.length - 5] ^= 0xff; // in the last block
  for (const cut of [100, packed.length]) {
    const chunks = [];
    const error = await failure(async () => {
      for await (const chunk of ReadableStream.from([packed.subarray(0, cut), packed.subarray(cut)]).pipeThrough(
        new DecompressStream(),
      )) {
        chunks.push(chunk);
      }
    });
    assert.ok(error instanceof FormatError, String(error));
    assertBytes(concat(chunks), text.subarray(0, 100_000), `ten blocks, cut at ${cut}`);
  }
});

test("a stream can be cancelled", async () => {
  for (const stream of [new CompressStream(), new DecompressStream()]) {
    const writer = stream.writable.getWriter();
    // Nothing reads the output, so this write waits; the cancel ends it with an error.
    const pending = writer.write(compress(text));
    await stream.readable.cancel("not needed");
    await assert.rejects(pending);
    await assert.rejects(writer.write(text));
  }
});

test("cancelling a stream that waits for input is safe, however often", async () => {
  // A reader that goes away while the input is slow: a cancel button, a client disconnecting.
  for (let i = 0; i < 10_000; i++) {
    for (const stream of i % 10 ? [new CompressStream()] : [new CompressStream(), new DecompressStream()]) {
      const reader = stream.readable.getReader();
      const read = reader.read();
      await new Promise((resolve) => setImmediate(resolve));
      await reader.cancel("gone");
      assert.deepEqual(await read, { done: true, value: undefined });
    }
  }
  assertBytes(decompress(compress(text)), text, "pawzip still works");
});

test("aborting the writing side errors the reading side", async () => {
  for (const stream of [new CompressStream(), new DecompressStream()]) {
    const reader = stream.readable.getReader();
    const read = reader.read();
    await stream.writable.getWriter().abort(new Error("stopped"));
    await assert.rejects(read, /stopped/);
  }
});

test("a writer may reuse its buffer once a write is done", async () => {
  const pattern = (i, size) => Uint8Array.from({ length: size }, (_, j) => (i * 31 + j * 7 + (j >> 9)) & 0xff);
  // Compressing, with one buffer refilled after each write.
  const compressing = new CompressStream({ blockSize: 1 << 16, modes: ["stored"] });
  const packed = Array.fromAsync(compressing.readable);
  const writer = compressing.writable.getWriter();
  const buffer = new Uint8Array(1 << 20);
  const written = [];
  for (let i = 0; i < 4; i++) {
    written.push(pattern(i, buffer.length));
    buffer.set(written[i]);
    await writer.write(buffer);
  }
  await writer.close();
  assertBytes(decompress(concat(await packed)), concat(written), "compressing");
  // Decompressing a .paw file written through one buffer.
  const data = pattern(9, 4 << 20);
  const paw = compress(data, { blockSize: 1 << 16, modes: ["stored"] });
  const decompressing = new DecompressStream();
  const out = Array.fromAsync(decompressing.readable);
  const pawWriter = decompressing.writable.getWriter();
  const pawBuffer = new Uint8Array(3 << 20);
  for (let at = 0; at < paw.length; at += pawBuffer.length) {
    const piece = paw.subarray(at, at + pawBuffer.length);
    pawBuffer.set(piece);
    await pawWriter.write(pawBuffer.subarray(0, piece.length));
  }
  await pawWriter.close();
  assertBytes(concat(await out), data, "decompressing");
});

test("with Node.js streams: files compressed and decompressed with pipeline", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pawzip-"));
  try {
    const original = Buffer.concat([randomBytes(50_000), Buffer.from(text)]);
    writeFileSync(join(dir, "notes.txt"), original);
    await pipeline(
      createReadStream(join(dir, "notes.txt")),
      Duplex.fromWeb(new CompressStream({ extension: "txt" })),
      createWriteStream(join(dir, "notes.paw")),
    );
    const packed = readFileSync(join(dir, "notes.paw"));
    assertBytes(packed, compress(original, { extension: "txt" }), "the .paw file");
    await pipeline(
      createReadStream(join(dir, "notes.paw")),
      Duplex.fromWeb(new DecompressStream()),
      createWriteStream(join(dir, "copy.txt")),
    );
    assertBytes(readFileSync(join(dir, "copy.txt")), original, "the copy");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
