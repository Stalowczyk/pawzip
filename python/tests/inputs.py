"""Deterministic test inputs shared by several test modules."""

import random
import struct

from paw import Mode
from paw.varint import write_varint


def bmp(width, height, pixel):
    """A 24-bit uncompressed BMP; pixel(x, y) -> (r, g, b)."""
    row_size = (width * 3 + 3) & ~3
    data = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            r, g, b = pixel(x, y)
            row += bytes((b, g, r))
        data += row + bytes(row_size - len(row))
    header = b"BM" + struct.pack("<I4xI", 54 + len(data), 54)
    header += struct.pack("<IiiHH24x", 40, width, height, 1, 24)
    return header + data


def _cases():
    rng = random.Random(1234)
    return {
        "empty": b"",
        "one byte": b"x",
        "two bytes": b"ab",
        "all 256 values": bytes(range(256)),
        "one letter": b"a" * 10_000,
        "alternating": b"ab" * 3000,
        "repeated line": b"hello world\n" * 500,
        "text": b"the cat and the hat sat on the mat. " * 200,
        "random": rng.randbytes(5000),
        "sparse": bytes(rng.choice([0] * 50 + [7]) for _ in range(8000)),
        "rgb stripes": bytes([10, 20, 30] * 200 + [255, 0, 0] * 300) * 10,
        "zeros then noise": bytes(4000) + rng.randbytes(500),
        "ramp": bytes((i // 7) % 256 for i in range(6000)),
        "narrow columns": b"aabbbb" * 1000,  # 'a' forms a nested series of 1000 rows, 2 wide
        "small image": bmp(48, 32, lambda x, y: (220, 30, 30) if 8 <= x < 30 and 4 <= y < 20 else (255,) * 3),
    }


CASES = _cases()


def huge_body_header():
    """A .paw file whose block header claims a 1 TiB body, followed by a single byte."""
    out = bytearray(b"PAW\x01")
    write_varint(out, 16)  # block size
    out.append(0)  # no stored extension
    out.append(0)  # no optional header data
    write_varint(out, 1)  # original size
    out += bytes([Mode.STORED]) + bytes(4)  # mode, CRC
    write_varint(out, 2**40)  # body size
    return bytes(out) + b"x"


def realistic_inputs():
    """Larger inputs resembling real files (used by slow tests)."""
    rng = random.Random(99)
    words = b"the of and to in is was for on that with as by at from his her it be".split()
    text = b" ".join(rng.choice(words) for _ in range(30000))
    csv = "".join(
        f"{i},2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d},{rng.uniform(1, 999):.2f}\n"
        for i in range(4000)
    ).encode()
    image = bmp(
        200, 150, lambda x, y: (x % 256, y % 256, 128) if (x // 25 + y // 25) % 2 else (250, 250, 250)
    )
    noisy = bmp(96, 96, lambda x, y: ((x + rng.randint(0, 9)) % 256, (y * 2) % 256, 60))
    return {"text": text, "csv": csv, "image": image, "noisy image": noisy, "random": rng.randbytes(40000)}
