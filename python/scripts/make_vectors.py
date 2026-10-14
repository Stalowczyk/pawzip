"""Regenerate spec/vectors/: small inputs and their .paw encodings in every mode.

Every implementation must decode each .paw file back to its input exactly. The Python
tests additionally check that encoding is unchanged. Format v1 is frozen, so
this script only adds new vectors: it fails if an existing file would change.

It also writes the hand-made vectors in spec/vectors/unusual/ and spec/vectors/invalid/
(see crafted_vectors.py).
"""

import hashlib
import json
import random
from pathlib import Path

import crafted_vectors
from crafted_vectors import write_frozen

from paw import DEFAULT_BLOCK_SIZE, Mode, compress

VECTORS = Path(__file__).resolve().parents[2] / "spec" / "vectors"
SMALL_BLOCK = 500  # exercises multi-block files

TEXT = (
    b"Compression is the art of saying the same thing with fewer bytes. A file full of the "
    b"letter a can be described in a handful of bytes, while a file of pure noise cannot be "
    b"described in fewer bytes than it already uses. Most real files sit somewhere in between: "
    b"they repeat words, lines, colours and layouts, and a compressor wins by noticing those "
    b"repetitions and describing them more cheaply than the data itself.\n"
) * 4


LONG_WORDS = b"the map series shape gap block paw byte value position".split()


def small_bmp(width=40, height=24):
    row_size = (width * 3 + 3) & ~3
    pixels = bytearray()
    for y in range(height):
        row = bytearray()
        for x in range(width):
            inside = 8 <= x < 28 and 6 <= y < 18
            row += bytes((40, 40, 220) if inside else (255, 255, 255))
        pixels += row + bytes(row_size - len(row))
    header = b"BM" + (54 + len(pixels)).to_bytes(4, "little") + bytes(4) + (54).to_bytes(4, "little")
    header += (40).to_bytes(4, "little") + width.to_bytes(4, "little") + height.to_bytes(4, "little")
    header += (1).to_bytes(2, "little") + (24).to_bytes(2, "little") + bytes(24)
    return header + pixels


def x86_code():
    """Bytes that look like x86 code: CALLs and JMPs to a few places, forwards and backwards,
    and some that the filter leaves alone."""
    rng = random.Random(86)
    targets = [rng.randrange(0, 4000) for _ in range(6)]
    out = bytearray()
    while len(out) < 4000:
        out += bytes(rng.choice([0x90, 0x55, 0x8B, 0xC3, 0x00]) for _ in range(rng.randrange(1, 6)))
        opcode = rng.choice([0xE8, 0xE8, 0xE8, 0xE9])
        if rng.random() < 0.1:  # top byte neither 00 nor FF: not converted
            out += bytes([opcode]) + rng.randbytes(3) + b"\x42"
        else:
            out += bytes([opcode]) + ((rng.choice(targets) - len(out) - 5) % 2**32).to_bytes(4, "little")
    return bytes(out) + b"\xe8\x01\x02"  # an E8 too close to the end


def inputs():
    rng = random.Random(2026)
    return {
        "empty": b"",
        "one-byte": b"x",
        "all-bytes": bytes(range(256)),
        "one-letter": b"a" * 5000,
        "repeated-line": b"hello world\n" * 300,
        "text": TEXT,
        "rgb-stripes": bytes([10, 20, 30] * 50 + [255, 0, 0] * 70) * 6,
        "small-image": small_bmp(),
        "sparse": bytes(rng.choice([0] * 40 + [7, 9]) for _ in range(3000)),
        "random": rng.randbytes(1000),
        # over 256 KiB: BWT blocks store walk rows (3 walks)
        "long-text": b" ".join(rng.choice(LONG_WORDS) for _ in range(90_000)),
        "x86-code": x86_code(),
    }


def main():
    VECTORS.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, data in inputs().items():
        write_frozen(VECTORS / f"{name}.bin", data)
        # one block with the default size, plus a multi-block variant for larger inputs
        block_sizes = [DEFAULT_BLOCK_SIZE] + ([SMALL_BLOCK] if len(data) > SMALL_BLOCK else [])
        modes = [Mode.STORED] if not data else list(Mode)
        if name == "long-text":  # only the BWT modes have walk rows; keep the repository small
            block_sizes = [DEFAULT_BLOCK_SIZE]
            modes = [m for m in Mode if m not in (Mode.STORED, Mode.MAP, Mode.DELTA_MAP)]
        for block_size in block_sizes:
            for mode in modes:
                suffix = "" if block_size == DEFAULT_BLOCK_SIZE else f".blocks{block_size}"
                paw_name = f"{name}.mode{int(mode)}{suffix}.paw"
                write_frozen(VECTORS / paw_name, compress(data, modes=(mode,), block_size=block_size))
                manifest.append(
                    {
                        "input": f"{name}.bin",
                        "paw": paw_name,
                        "mode": int(mode),
                        "block_size": block_size,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
    (VECTORS / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", newline="\n")
    crafted = crafted_vectors.write(VECTORS)
    print(f"checked {len(manifest)} vectors and {crafted} hand-made ones in {VECTORS}")


if __name__ == "__main__":
    main()
