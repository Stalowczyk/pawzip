// What npm publishes: the built files, the README and the license, and nothing else.
import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const js = new URL("../", import.meta.url);

test("the package holds dist/, the README and the license", () => {
  const [pack] = JSON.parse(execSync("npm pack --dry-run --json --ignore-scripts", { cwd: js, encoding: "utf8" }));
  assert.deepEqual(pack.files.map((f) => f.path).sort(), [
    "LICENSE",
    "README.md",
    "dist/index.d.ts",
    "dist/index.js",
    "dist/pawzip_wasm.base64.js",
    "dist/pawzip_wasm.js",
    "package.json",
  ]);
});

test("the version matches the Rust workspace's, like the Python package's", () => {
  const cargo = readFileSync(new URL("../rust/Cargo.toml", js), "utf8");
  const version = /^version = "(.+)"$/m.exec(cargo)[1];
  assert.equal(JSON.parse(readFileSync(new URL("package.json", js), "utf8")).version, version);
});
