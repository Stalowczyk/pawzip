// The shared test vectors (spec/vectors): what every implementation must pass. The streams
// must give the same results as the functions, wherever their input is cut.
import assert from "node:assert/strict";
import { test } from "node:test";
import { CompressStream, DecompressStream, FormatError, PawError, compress, decompress, info } from "pawzip";
import { MODES, assertBytes, failure, manifest, pipe, vector } from "./helpers.js";

const valid = manifest("manifest.json");
const unusual = manifest("unusual/manifest.json");
const invalid = manifest("invalid/manifest.json");

test("every vector decodes to its input", () => {
  for (const v of valid) {
    assertBytes(decompress(vector(v.paw)), vector(v.input), v.paw);
    assert.equal(info(vector(v.paw)).originalSize, v.size, v.paw);
  }
  assert.equal(valid.length, 150);
});

test("the encoder writes exactly the bytes of every vector", () => {
  for (const v of valid) {
    const options = { modes: [MODES[v.mode]], blockSize: v.block_size };
    assertBytes(compress(vector(v.input), options), vector(v.paw), v.paw);
  }
});

test("valid files the encoder never writes decode", () => {
  for (const v of unusual) {
    assertBytes(decompress(vector(`unusual/${v.paw}`)), vector(`unusual/${v.input}`), v.paw);
  }
  assert.equal(unusual.length, 9);
});

test("every invalid file is refused with a FormatError", () => {
  for (const v of invalid) {
    assert.throws(
      () => decompress(vector(`invalid/${v.paw}`)),
      (e) => e instanceof FormatError && e instanceof PawError && e.message.length > 0,
      `${v.paw}: ${v.rule}`,
    );
  }
  assert.equal(invalid.length, 42);
});

test("the streams give the same results, cut anywhere", async () => {
  for (const cut of [3, 4096]) {
    for (const v of valid.filter((v) => v.size < 100_000)) {
      const options = { modes: [MODES[v.mode]], blockSize: v.block_size };
      const name = `${v.paw}, pieces of ${cut}`;
      assertBytes(await pipe(vector(v.input), new CompressStream(options), cut), vector(v.paw), name);
      assertBytes(await pipe(vector(v.paw), new DecompressStream(), cut), vector(v.input), name);
    }
    for (const v of invalid) {
      const data = vector(`invalid/${v.paw}`);
      const expected = await failure(() => decompress(data));
      const error = await failure(() => pipe(data, new DecompressStream(), cut));
      assert.ok(error instanceof FormatError, `${v.paw}, pieces of ${cut}: ${error}`);
      assert.equal(error.message, expected.message, `${v.paw}, pieces of ${cut}`);
    }
  }
});
