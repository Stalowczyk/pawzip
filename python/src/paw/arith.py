"""Adaptive range coding (32-bit, byte-wise, LZMA style), plus a separate stream of raw bits.

The encoder narrows a range [low, low + range) for every coded symbol, in proportion to the
symbol's probability, and moves out a whole byte whenever the range drops below 2**24. Bits
with a fixed probability of 1/2 (the unpredicted extra bits of numbers) skip the coder and go
to a plain bit stream. Encoder and decoder must follow exactly the same arithmetic, so every
constant here is part of the format.
"""

from __future__ import annotations

from .errors import FormatError
from .varint import Reader, write_varint

RANGE_BOTTOM = 1 << 24  # below this, the coder moves a byte out
NUM_CLASSES = 64
INCREMENT = 64
LIMIT = 1 << 14

BIT_ONE = 1 << 12  # bit models hold the probability of a 0 in units of 1/BIT_ONE
BIT_SHIFT = 6  # each coded bit moves that probability 1/64 of the way towards it


class BitModel:
    """Adaptive probability of a single bit being 0."""

    __slots__ = ("p",)

    def __init__(self) -> None:
        self.p = BIT_ONE // 2

    def update(self, bit: int) -> None:
        if bit:
            self.p -= self.p >> BIT_SHIFT
        else:
            self.p += (BIT_ONE - self.p) >> BIT_SHIFT


class Model:
    """Adaptive frequency table over NUM_CLASSES symbols."""

    def __init__(self, freq: list[int] | None = None) -> None:
        self.freq = list(freq) if freq else [1] * NUM_CLASSES
        self.total = sum(self.freq)

    def range_of(self, sym: int) -> tuple[int, int]:
        low = sum(self.freq[:sym])
        return low, low + self.freq[sym]

    def find(self, count: int) -> tuple[int, int, int]:
        low = 0
        for sym, f in enumerate(self.freq):
            if count < low + f:
                return sym, low, low + f
            low += f
        raise FormatError("corrupt range-coded stream")

    def update(self, sym: int) -> None:
        self.freq[sym] += INCREMENT
        self.total += INCREMENT
        if self.total > LIMIT:
            self.freq = [(f + 1) // 2 for f in self.freq]
            self.total = sum(self.freq)


def _trim(data: bytes | bytearray) -> bytes:
    """Drop trailing zero bytes: decoders read zeros past the end of each stream."""
    return bytes(data).rstrip(bytes(1))


class Encoder:
    def __init__(self) -> None:
        self.low = 0  # may reach 2**32: the carry goes into the bytes already waiting
        self.range = 0xFFFFFFFF
        self.cache = 0  # the last byte not yet written (a carry may still change it)
        self.cache_size = 1  # that byte plus the 0xFF bytes after it, also waiting
        self.out = bytearray()
        self.raw: list[int] = []

    def _shift_low(self) -> None:
        if self.low < 0xFF000000 or self.low > 0xFFFFFFFF:
            carry = self.low >> 32
            byte = self.cache
            for _ in range(self.cache_size):
                self.out.append((byte + carry) & 0xFF)
                byte = 0xFF
            self.cache_size = 0
            self.cache = (self.low >> 24) & 0xFF
        self.cache_size += 1
        self.low = (self.low & 0x00FFFFFF) << 8

    def _normalize(self) -> None:
        while self.range < RANGE_BOTTOM:
            self.range <<= 8
            self._shift_low()

    def encode(self, model: Model, sym: int) -> None:
        lo, hi = model.range_of(sym)
        r = self.range // model.total
        self.low += r * lo
        self.range = r * (hi - lo)
        self._normalize()
        model.update(sym)

    def encode_bit(self, bit: int) -> None:
        """A bit with fixed probability 1/2, written to the raw bit stream."""
        self.raw.append(bit)

    def encode_adaptive_bit(self, model: BitModel, bit: int) -> None:
        bound = (self.range >> 12) * model.p
        if bit:
            self.low += bound
            self.range -= bound
        else:
            self.range = bound
        self._normalize()
        model.update(bit)

    def finish(self) -> bytes:
        """Flush and return the coded stream: varint length of the range-coded bytes, those
        bytes, then the raw bits packed MSB-first."""
        for _ in range(5):
            self._shift_low()
        coded = _trim(self.out[1:])  # the first byte is always 0
        raw = bytearray((len(self.raw) + 7) // 8)
        for i, bit in enumerate(self.raw):
            if bit:
                raw[i >> 3] |= 0x80 >> (i & 7)
        out = bytearray()
        write_varint(out, len(coded))
        return bytes(out) + coded + _trim(raw)


class Decoder:
    def __init__(self, data: bytes) -> None:
        r = Reader(data)
        size = r.varint()
        if size > len(data) - r.pos:
            raise FormatError("range-coded part longer than the map body")
        self.data = data[r.pos : r.pos + size]
        self.raw = data[r.pos + size :]
        self.pos = 0
        self.raw_pos = 0
        self.range = 0xFFFFFFFF
        self.code = 0
        for _ in range(4):
            self.code = (self.code << 8) | self._byte()

    def _byte(self) -> int:
        i = self.pos
        self.pos += 1
        return self.data[i] if i < len(self.data) else 0  # past the end: zeros

    def _normalize(self) -> None:
        while self.range < RANGE_BOTTOM:
            self.range <<= 8
            self.code = ((self.code << 8) | self._byte()) & 0xFFFFFFFF

    def decode(self, model: Model) -> int:
        r = self.range // model.total
        count = min(self.code // r, model.total - 1)  # only damaged data goes past the end
        sym, lo, hi = model.find(count)
        self.code -= r * lo
        self.range = r * (hi - lo)
        self._normalize()
        model.update(sym)
        return sym

    def decode_bit(self) -> int:
        i = self.raw_pos
        self.raw_pos += 1
        if i >= len(self.raw) * 8:
            return 0
        return (self.raw[i >> 3] >> (7 - (i & 7))) & 1

    def decode_adaptive_bit(self, model: BitModel) -> int:
        bound = (self.range >> 12) * model.p
        if self.code < bound:
            bit = 0
            self.range = bound
        else:
            bit = 1
            self.code -= bound
            self.range -= bound
        self._normalize()
        model.update(bit)
        return bit
