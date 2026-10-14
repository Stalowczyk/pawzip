"""Unsigned LEB128 varints: 7 bits per byte, lowest bits first, at most 64 bits."""

from __future__ import annotations

from collections.abc import Callable

from .errors import FormatError

MAX_VARINT_BYTES = 10  # enough for any value below 2**64


def varint_len(n: int) -> int:
    size = 1
    while n >= 0x80:
        n >>= 7
        size += 1
    return size


def write_varint(out: bytearray, n: int) -> None:
    if n < 0:
        raise ValueError("varints are unsigned")
    if n >> 64:
        raise ValueError("varints hold at most 64 bits")
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def read_varint(next_byte: Callable[[], int]) -> int:
    """Read one varint, pulling bytes from ``next_byte``. Non-minimal encodings are fine;
    more than 10 bytes, or a value of 2**64 or more, is a FormatError."""
    result = shift = 0
    while True:
        b = next_byte()
        result |= (b & 0x7F) << shift
        if b < 0x80:
            break
        shift += 7
        if shift >= 7 * MAX_VARINT_BYTES:
            raise FormatError("varint longer than 10 bytes")
    if result >> 64:
        raise FormatError("varint larger than 64 bits")
    return result


class Reader:
    """Sequential reader over a bytes object."""

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise FormatError("unexpected end of data")
        self.pos += 1
        return self.data[self.pos - 1]

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise FormatError("unexpected end of data")
        self.pos += count
        return self.data[self.pos - count : self.pos]

    def varint(self) -> int:
        return read_varint(self.byte)
