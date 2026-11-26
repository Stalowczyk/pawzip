// Shared by the tests: the spec's test vectors, and streams fed in pieces.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const vectors = new URL("../../spec/vectors/", import.meta.url);

/** The modes by the byte that stands for each, as in the vector manifests. */
export const MODES = [
  "stored",
  "map",
  "bwt-mtf-map",
  "delta-map",
  "delta-bwt-mtf-map",
  "bwt-map",
  "delta-bwt-map",
  "x86-bwt-map",
];

export function vector(path) {
  return new Uint8Array(readFileSync(new URL(path, vectors)));
}

export function manifest(path) {
  return JSON.parse(readFileSync(new URL(path, vectors), "utf8"));
}

/** Compare bytes with a short message instead of a dump of both arrays. */
export function assertBytes(actual, expected, message) {
  assert.ok(actual instanceof Uint8Array, `${message}: not a Uint8Array`);
  assert.equal(actual.length, expected.length, `${message}: length`);
  assert.ok(
    Buffer.compare(Buffer.from(actual.buffer, actual.byteOffset, actual.length), Buffer.from(expected)) === 0,
    `${message}: bytes differ`,
  );
}

/** Run `data` through a TransformStream in pieces of `cut` bytes; the output, joined. */
export async function pipe(data, transform, cut = 65536) {
  const pieces = [];
  for (let i = 0; i < data.length; i += cut) pieces.push(data.subarray(i, i + cut));
  const chunks = await Array.fromAsync(ReadableStream.from(pieces).pipeThrough(transform));
  for (const chunk of chunks) assert.ok(chunk instanceof Uint8Array, "a stream gives Uint8Arrays");
  return concat(chunks);
}

export function concat(chunks) {
  const out = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.length;
  }
  return out;
}

/** The error a call throws, or a rejected promise gives: fails if there is none. */
export async function failure(f) {
  try {
    await f();
  } catch (e) {
    return e;
  }
  assert.fail("expected an error");
}

/** A number as a .paw varint: 7 bits per byte, the lowest first. */
export function varint(n) {
  const out = [];
  for (; n >= 0x80; n = Math.floor(n / 0x80)) out.push((n % 0x80) | 0x80);
  out.push(n);
  return out;
}
