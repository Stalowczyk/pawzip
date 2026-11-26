// CommonJS code loads the package with require() too (Node.js 22.12 and later), and gets the
// same module as import does.
const assert = require("node:assert/strict");
const { test } = require("node:test");

test("require() gives the module import gives", async () => {
  const required = require("pawzip");
  const imported = await import("pawzip");
  assert.equal(required.compress, imported.compress);
  assert.equal(required.FormatError, imported.FormatError);
  const data = new TextEncoder().encode("hello from CommonJS\n".repeat(100));
  assert.deepEqual(required.decompress(required.compress(data)), data);
});
