// Builds dist/, the package as published: the Rust core (rust/pawzip-wasm) compiled to
// WebAssembly and inlined as base64, the JavaScript glue wasm-bindgen writes for it, and the
// API and its types from src/.
//
// Needs Rust with the wasm32-unknown-unknown target, and wasm-bindgen-cli in the version of
// the wasm-bindgen crate in rust/Cargo.lock.
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const js = fileURLToPath(new URL("..", import.meta.url));
const rust = fileURLToPath(new URL("../../rust", import.meta.url));
const dist = join(js, "dist");

const lock = readFileSync(join(rust, "Cargo.lock"), "utf8");
const wanted = /\nname = "wasm-bindgen"\r?\nversion = "([^"]+)"/.exec(lock)?.[1];
let installed = "none";
try {
  installed = execFileSync("wasm-bindgen", ["--version"], { encoding: "utf8" }).trim().split(" ").pop();
} catch {
  // not installed: reported below
}
if (installed !== wanted) {
  console.error(
    `The build needs wasm-bindgen-cli ${wanted}, the version in rust/Cargo.lock; found: ${installed}.\n` +
      `Install it with: cargo install wasm-bindgen-cli --version ${wanted} --locked`,
  );
  process.exit(1);
}

execFileSync("cargo", ["build", "--release", "--target", "wasm32-unknown-unknown", "-p", "pawzip-wasm"], {
  cwd: rust,
  stdio: "inherit",
});
const module = join(rust, "target", "wasm32-unknown-unknown", "release", "pawzip_wasm.wasm");
const glue = mkdtempSync(join(tmpdir(), "pawzip-wasm-"));
try {
  // --omit-default-module-path: the glue doesn't point at a .wasm file of its own, which
  // bundlers would otherwise try to find. The module is passed to it inlined (src/index.js).
  execFileSync(
    "wasm-bindgen",
    ["--target", "web", "--omit-default-module-path", "--no-typescript", "--out-dir", glue, module],
    { stdio: "inherit" },
  );
  rmSync(dist, { recursive: true, force: true });
  mkdirSync(dist);
  copyFileSync(join(glue, "pawzip_wasm.js"), join(dist, "pawzip_wasm.js"));
  const wasm = readFileSync(join(glue, "pawzip_wasm_bg.wasm"));
  writeFileSync(
    join(dist, "pawzip_wasm.base64.js"),
    `// The pawzip WebAssembly module, ${wasm.length} bytes, in base64.\n` +
      `export default "${wasm.toString("base64")}";\n`,
  );
  for (const file of ["index.js", "index.d.ts"]) copyFileSync(join(js, "src", file), join(dist, file));
  console.log(`dist/ is ready: a ${Math.round(wasm.length / 1024)} KiB WebAssembly module`);
} finally {
  rmSync(glue, { recursive: true, force: true });
}
