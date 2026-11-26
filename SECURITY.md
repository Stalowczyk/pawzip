# Security

## Supported versions

Security fixes go into a new release of each package: `pawzip` on PyPI, and `pawzip` and
`pawzip-cli` on crates.io. Upgrade to the latest release to get them. The `.paw` format is
frozen at version 1, so upgrading never changes what a file decodes to.

## Reporting a problem

Please report security problems privately, through
[GitHub's private vulnerability reporting](https://github.com/Stalowczyk/pawzip/security/advisories/new),
not in a public issue. Say which package and version you used, what you ran, and attach a file
that shows the problem if you have one.

Decompressing is meant to be safe on untrusted input: a damaged or hostile `.paw` file should
fail with an error rather than crash or hang, and the size limits (`max_size` and
`max_block_size`) should hold, so that a small file can't make the decoder use much memory.
Anything that breaks this counts as a security problem.
