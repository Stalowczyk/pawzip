# Contributing

Bug reports, ideas and pull requests are all welcome.

## Before you start

- Report bugs and suggest ideas in [issues](https://github.com/Stalowczyk/pawzip/issues).
- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Found a security problem? Please
  [report it privately](https://github.com/Stalowczyk/pawzip/security/advisories/new) instead of
  opening an issue.

## Use of AI

AI tools are fine to use. You're responsible for what you submit: understand it, test it and be
able to explain it in your own words. If AI wrote a significant part of a change, say so in the
pull request.

## Setup

Requires Python 3.10+ and [Rust](https://rustup.rs).

```sh
python -m venv .venv
.venv/bin/python -m pip install -e "python[dev]"   # Scripts/ instead of bin/ on Windows
.venv/bin/pre-commit install
```

Run the install again after changing Rust code.

The JavaScript package (`js/`) also needs [Node.js](https://nodejs.org) 22.12+, Rust's
WebAssembly target, and `wasm-bindgen-cli` in the version of `wasm-bindgen` in
`rust/Cargo.lock` (the build says how to install it when it's missing):

```sh
rustup target add wasm32-unknown-unknown
cd js && npm ci && npm run build
```

Run `npm run build` again after changing Rust code.

## Tests

```sh
.venv/bin/python -m pytest python                  # with the Rust core
PAW_PURE=1 .venv/bin/python -m pytest python       # with the pure-Python code
cd rust && cargo test --release -p pawzip -p pawzip-cli
cd js && npm test && npm run check                 # the npm package, and its types
cd js && npm run test:browser                      # a quick check in headless Chrome
```

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep each pull request to one change, with tests. A bug fix needs a test that fails without
   it.
3. Change compression code in both Python (`python/src/paw`) and Rust (`rust/pawzip`): the two
   must write identical files.
4. Add a line under `## [Unreleased]` in `CHANGELOG.md` for anything users will notice.
5. Commit using [Conventional Commits](https://www.conventionalcommits.org).
6. Open a pull request against `main`. It's merged once CI passes and the change has been
   reviewed and approved.

The `.paw` format is frozen at version 1, so changes to it, or to the files in `spec/vectors/`,
can't be accepted.

## Repository layout

```
spec/          the .paw format spec + test vectors every implementation must pass
python/        the Python package (`pawzip`, import `paw`): API, CLI and the pure-Python reference
rust/          the Rust library (pawzip), the package's native module (pawzip-py), a
               standalone `paw` command (pawzip-cli) and the npm package's engine (pawzip-wasm)
js/            the npm package (`pawzip`): the JavaScript API and its types, over pawzip-wasm
docs/          how it works, with the diagrams
```
