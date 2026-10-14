"""Differential test: the Rust core against the Python reference implementation.

    python rust/differential.py [--cases N] [--seed S]

Generates N varied inputs and options, compresses them with Python, and checks that Rust
writes the same bytes. Then damages every Python-made file in several ways and checks that
both implementations reach the same outcome (same data, or the same kind of error) and that
Rust never panics. Needs the dev environment (`pip install -e python`) and cargo.
"""

import argparse
import random
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import paw

RUST = Path(__file__).resolve().parent
MODES = list(paw.Mode)


def make_input(rng: random.Random) -> bytes:
    kind = rng.choice(["random", "alphabet", "image", "sparse", "ramp", "text", "empty-ish"])
    size = rng.choice([0, 1, 2, 3, 10, 100, 1000, 5000, 20000])
    if kind == "random":
        return rng.randbytes(size)
    if kind == "alphabet":
        letters = rng.randbytes(rng.randint(1, 5))
        chunk = bytes(rng.choice(letters) for _ in range(rng.randint(1, 60)))
        return (chunk * (size // max(1, len(chunk)) + 1))[:size]
    if kind == "image":
        width, height = rng.randint(4, 120), rng.randint(2, 60)
        pixels = bytearray(rng.choice(b"\x00\xff") for _ in range(width * height * 3))
        for _ in range(rng.randint(1, 6)):  # rectangles of a colour
            x, y = rng.randrange(width), rng.randrange(height)
            w, h = rng.randint(1, width - x), rng.randint(1, height - y)
            colour = rng.randbytes(3)
            for row in range(y, y + h):
                start = (row * width + x) * 3
                pixels[start : start + w * 3] = colour * w
        return bytes(pixels)
    if kind == "sparse":
        return bytes(rng.choice([0] * 30 + [7, 9, 200]) for _ in range(size))
    if kind == "ramp":
        step = rng.randint(1, 9)
        return bytes((i // step) % 256 for i in range(size))
    if kind == "text":
        words = b"the of and paw series map gap shape block".split()
        return b" ".join(rng.choice(words) for _ in range(size // 4))
    return bytes(size)


def make_options(rng: random.Random) -> tuple[str, dict]:
    modes = None if rng.random() < 0.5 else sorted(set(rng.sample(MODES, rng.randint(1, 3))))
    block_size = rng.choice([1, 7, 64, 500, 4096, 1 << 24])
    thorough = rng.random() < 0.2
    extension = rng.choice([None, "png", "txt", "tar_gz", "X-1"])
    spec = (
        f"{'auto' if modes is None else ','.join(str(int(m)) for m in modes)} "
        f"{block_size} {int(thorough)} {extension or '-'}"
    )
    return spec, {"modes": modes, "block_size": block_size, "thorough": thorough, "extension": extension}


def mutations(rng: random.Random, blob: bytes) -> list[bytes]:
    out = []
    for _ in range(4):
        b = bytearray(blob)
        action = rng.choice(["flip", "byte", "truncate", "append", "header", "insert"])
        if action == "flip" and b:
            i = rng.randrange(len(b))
            b[i] ^= 1 << rng.randrange(8)
        elif action == "byte" and b:
            b[rng.randrange(len(b))] = rng.randrange(256)
        elif action == "truncate":
            del b[rng.randrange(len(b) + 1) :]
        elif action == "append":
            b += rng.randbytes(rng.randint(1, 8))
        elif action == "header" and len(b) > 5:
            b[rng.randrange(4, min(len(b), 12))] = rng.randrange(256)
        elif action == "insert":
            i = rng.randrange(len(b) + 1)
            b[i:i] = rng.randbytes(rng.randint(1, 4))
        out.append(bytes(b))
    return out


def python_outcome(blob: bytes) -> str:
    try:
        data = paw.decompress(blob)
    except paw.SizeLimitError:
        return "err sizelimit"
    except paw.FormatError:
        return "err format"
    return f"ok {len(data)} {zlib.crc32(data):08x}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=500)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    subprocess.run(["cargo", "build", "-q", "--release", "--example", "batch"], cwd=RUST, check=True)
    batch = RUST / "target" / "release" / "examples" / ("batch.exe" if sys.platform == "win32" else "batch")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        expected: dict[str, tuple[bytes, str]] = {}
        outcomes: dict[str, str] = {}
        for case in range(args.cases):
            data = make_input(rng)
            spec, opts = make_options(rng)
            blob = paw.compress(data, **opts)
            assert paw.decompress(blob) == data
            (work / f"c{case}.in").write_bytes(data)
            (work / f"c{case}.opt").write_text(spec)
            expected[f"c{case}"] = (blob, spec)
            for k, damaged in enumerate(mutations(rng, blob)):
                name = f"c{case}m{k}"
                (work / f"{name}.mut").write_bytes(damaged)
                outcomes[name] = python_outcome(damaged)

        subprocess.run([str(batch), str(work)], check=True)

        encode_mismatches = [
            (name, spec)
            for name, (blob, spec) in expected.items()
            if (work / f"{name}.rs.paw").read_bytes() != blob
        ]
        decode_mismatches = []
        panics = 0
        for name, outcome in outcomes.items():
            rust = (work / f"{name}.rs.out").read_text()
            panics += rust == "err panic"
            if rust != outcome:
                decode_mismatches.append((name, outcome, rust))

    print(f"{len(expected)} inputs: {len(expected) - len(encode_mismatches)} encoded identically")
    same = len(outcomes) - len(decode_mismatches)
    print(f"{len(outcomes)} damaged files: {same} same outcome, {panics} panics")
    for name, spec in encode_mismatches[:10]:
        print(f"  ENCODE DIFFERS {name} ({spec})")
    for name, py, rs in decode_mismatches[:10]:
        print(f"  DECODE DIFFERS {name}: python {py!r}, rust {rs!r}")
    return 1 if encode_mismatches or decode_mismatches or panics else 0


if __name__ == "__main__":
    sys.exit(main())
