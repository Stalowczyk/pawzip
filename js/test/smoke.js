// A quick check that the package works in a JavaScript runtime other than Node.js (which runs
// the full tests): Deno, Bun, or a browser through test/browser.html. Throws if anything is
// wrong. Only web APIs are used, so it runs anywhere unchanged.
import { CompressStream, DecompressStream, FormatError, compress, decompress, info } from "../dist/index.js";

const data = new TextEncoder().encode("hello world, hello paw\n".repeat(5000));
const options = { extension: "txt", blockSize: 40_000 };
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const collect = async (stream) => new Uint8Array(await new Response(stream).arrayBuffer());

const packed = compress(data, options);
if (!same(decompress(packed), data)) throw new Error("compress, then decompress, changed the data");
if (info(packed).extension !== "txt" || info(packed).modes.length !== 3) throw new Error("info is wrong");

const streamed = await collect(new Blob([data]).stream().pipeThrough(new CompressStream(options)));
if (!same(streamed, packed)) throw new Error("CompressStream wrote different bytes than compress");
const restored = await collect(new Blob([packed]).stream().pipeThrough(new DecompressStream()));
if (!same(restored, data)) throw new Error("DecompressStream changed the data");

let refused = false;
try {
  decompress(new TextEncoder().encode("not a .paw file"));
} catch (e) {
  refused = e instanceof FormatError;
}
if (!refused) throw new Error("a bad file wasn't refused with a FormatError");

console.log(`pawzip works: ${data.length} bytes compressed to ${packed.length}, streams included`);
