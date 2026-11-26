// Runs test/smoke.js in headless Chrome, on the page's main thread: npm run test:browser.
// Set CHROME to the browser's executable if it isn't in the usual place.
import { execFile } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const types = { ".html": "text/html", ".js": "text/javascript" };
const server = createServer((request, response) => {
  const path = normalize(join(root, decodeURIComponent(new URL(request.url, "http://localhost").pathname)));
  if (!path.startsWith(root + sep) || !types[extname(path)] || !existsSync(path)) {
    response.writeHead(404).end();
    return;
  }
  response.writeHead(200, { "content-type": types[extname(path)] }).end(readFileSync(path));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));

const chrome =
  [
    process.env.CHROME,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].find((path) => path && existsSync(path)) ?? "google-chrome";
const profile = mkdtempSync(join(tmpdir(), "pawzip-chrome-"));
const args = [
  "--headless=new",
  "--disable-gpu",
  "--no-first-run",
  "--no-default-browser-check",
  `--user-data-dir=${profile}`,
  "--virtual-time-budget=60000",
  "--dump-dom",
  `http://127.0.0.1:${server.address().port}/test/browser.html`,
];
// CI runners don't allow Chrome's sandbox; the page is this package's own test.
if (process.env.CI) args.unshift("--no-sandbox");

try {
  const page = await new Promise((resolve, reject) => {
    execFile(chrome, args, { timeout: 120_000 }, (error, stdout) => (error ? reject(error) : resolve(stdout)));
  });
  const title = /<title>(.*?)<\/title>/s.exec(page)?.[1] ?? "(no title)";
  console.log(`${chrome}: ${title}`);
  if (title !== "ok") {
    console.log(`The page:\n${page || "(empty)"}`);
    process.exitCode = 1;
  }
} finally {
  server.close();
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
