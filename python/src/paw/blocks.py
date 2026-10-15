"""Encoding and decoding of one block: mode selection and the per-mode body."""

from __future__ import annotations

import math
import os
from collections import Counter
from collections.abc import Iterable
from enum import IntEnum

from .errors import FormatError
from .gaps import float_sum
from .mapcodec import decode_map, encode_map
from .transforms import (
    bwt_with_rows,
    delta,
    inverse_bwt,
    inverse_delta,
    inverse_mtf,
    mtf,
    walk_count,
    x86_call_sites,
    x86_calls,
)
from .varint import Reader, write_varint

try:  # the Rust core, when the package was built with it; PAW_PURE=1 forces pure Python
    if os.environ.get("PAW_PURE"):
        raise ImportError
    from . import _core
except ImportError:
    _core = None  # type: ignore[assignment]

NATIVE = _core is not None
"""True when blocks are coded by the Rust core, False for the pure-Python reference."""


class Mode(IntEnum):
    """How a block was encoded (stored in each block's header)."""

    STORED = 0
    """The original bytes, unchanged. Used when nothing else makes the block smaller."""
    MAP = 1
    """Position map with series. Best for structured data: images, records, repetition."""
    BWT_MTF_MAP = 2
    """Burrows-Wheeler + move-to-front, then the map. Often best for small files."""
    DELTA_MAP = 3
    """Delta filter, then the map. For smooth data such as gradients and simple audio."""
    DELTA_BWT_MTF_MAP = 4
    """Delta filter, then BWT + MTF and the map."""
    BWT_MAP = 5
    """Burrows-Wheeler, then the map of its output. Best for text and most larger files."""
    DELTA_BWT_MAP = 6
    """Delta filter, then BWT and the map."""
    X86_BWT_MAP = 7
    """x86 call filter, then BWT and the map. For executables and libraries (x86 code)."""

    @property
    def label(self) -> str:
        """Short human-readable name, e.g. ``"bwt-mtf-map"``."""
        return self.name.lower().replace("_", "-")


DELTA_MODES = (Mode.DELTA_MAP, Mode.DELTA_BWT_MTF_MAP, Mode.DELTA_BWT_MAP)
BWT_MODES = (Mode.BWT_MTF_MAP, Mode.DELTA_BWT_MTF_MAP, Mode.BWT_MAP, Mode.DELTA_BWT_MAP, Mode.X86_BWT_MAP)
MTF_MODES = (Mode.BWT_MTF_MAP, Mode.DELTA_BWT_MTF_MAP)
X86_MODES = (Mode.X86_BWT_MAP,)
BASIC_MODES = (Mode.STORED, Mode.MAP, Mode.BWT_MTF_MAP, Mode.BWT_MAP)
DELTA_DISTANCES = (1, 2, 3, 4)
# The x86 mode is tried on blocks with at least one call the filter converts per this many
# bytes: the benchmark executables have 4 to 14, other data at most about 0.35.
X86_BYTES_PER_CALL = 512


def resolve_modes(modes: Iterable[Mode | int] | None) -> tuple[Mode, ...] | None:
    """Validate a user-supplied list of modes (``None`` means automatic)."""
    if modes is None:
        return None
    try:
        resolved = tuple(Mode(m) for m in modes)
    except ValueError as e:
        raise ValueError(f"unknown mode: {e}") from None
    if not resolved:
        raise ValueError("modes must not be empty")
    return resolved


def _entropy(data: bytes) -> float:
    """Order-0 entropy estimate in bytes (only used to decide what is worth trying)."""
    n = len(data)
    return float_sum(c * math.log2(n / c) for c in Counter(data).values()) / 8


def _pick_delta(data: bytes) -> tuple[int, bool]:
    """Return (best delta distance, whether it looks better than no filter)."""
    scores = {d: _entropy(delta(data, d)) for d in DELTA_DISTANCES}
    best = min(scores, key=scores.__getitem__)
    return best, scores[best] < _entropy(data)


def _looks_like_x86(data: bytes) -> bool:
    """True if ``data`` looks like x86 machine code: enough calls the x86 filter converts."""
    calls = sum(1 for _ in x86_call_sites(data))
    return calls * X86_BYTES_PER_CALL >= len(data)


Bwt = tuple[bytes, int, list[int]]  # (last column, primary index, walk rows)


def _body(data: bytes, mode: Mode, distance: int, bwts: dict[str, Bwt] | None = None) -> bytes:
    """The body of ``data`` in ``mode``. ``bwts`` keeps the BWT of each filter ("delta",
    "x86" or ""), so the modes with and without MTF sort only once."""
    if mode == Mode.STORED:
        return data
    out = bytearray()
    kind = ""
    if mode in DELTA_MODES:
        write_varint(out, distance)
        data, kind = delta(data, distance), "delta"
    elif mode in X86_MODES:
        data, kind = x86_calls(data, forward=True), "x86"
    if mode not in BWT_MODES:
        return bytes(out) + encode_map(data)
    bwts = {} if bwts is None else bwts
    if kind not in bwts:
        bwts[kind] = bwt_with_rows(data)
    last, primary, rows = bwts[kind]
    write_varint(out, primary)
    for row in rows:
        write_varint(out, row)
    if mode in MTF_MODES:
        return bytes(out) + encode_map(mtf(last))
    return bytes(out) + encode_map(last, bwt_output=True)


# Already-compressed data (JPEG, ZIP, video) would take minutes to try in every mode, only
# to be stored. A sample of a few slices predicts that well: on standard benchmark files real
# data samples to at most ~81% of its size, while compressed formats sample to 97-101%.
PROBE_MIN = 256 * 1024  # smaller blocks are quick to try in full
PROBE_SLICE = 16 * 1024
PROBE_SLICES = 4
PROBE_GIVE_UP = 0.95
# The same sample says when a mode without BWT can't win: it is tried on the whole block
# only if its sample is at most 15% larger than with BWT. Samples favour the modes without
# BWT (BWT gains more from a long context); on the benchmarks the plain map won only where
# its sample was at most 4% larger, and lost by 2x or more on text.
PROBE_SKIP_PERCENT = 115


def _sample(data: bytes) -> bytes:
    step = (len(data) - PROBE_SLICE) // (PROBE_SLICES - 1)
    return b"".join(data[i * step : i * step + PROBE_SLICE] for i in range(PROBE_SLICES))


def _beaten(without: int, with_bwt: int) -> bool:
    """True if a sample body without BWT is too large to win on the whole block."""
    return 100 * without > PROBE_SKIP_PERCENT * with_bwt


def _looks_incompressible(data: bytes) -> bool:
    """True if a sample of ``data`` can't be squeezed below PROBE_GIVE_UP of its size."""
    if len(data) < PROBE_MIN:
        return False
    sample = _sample(data)
    best = min(len(_body(sample, mode, 0)) for mode in (Mode.MAP, Mode.BWT_MTF_MAP))
    return best >= PROBE_GIVE_UP * len(sample)


def compress_block(data: bytes, modes: tuple[Mode, ...] | None, thorough: bool = False) -> tuple[Mode, bytes]:
    """Encode one non-empty block in every candidate mode; return the smallest (with the
    Rust core when available: same bytes, much faster)."""
    if _core is not None:
        codes = None if modes is None else [int(m) for m in modes]
        mode, body = _core.compress_block(bytes(data), codes, thorough)
        return Mode(mode), body
    return compress_block_pure(data, modes, thorough)


def compress_block_pure(
    data: bytes, modes: tuple[Mode, ...] | None, thorough: bool = False
) -> tuple[Mode, bytes]:
    """The pure-Python reference of :func:`compress_block`.

    With automatic modes and a block of PROBE_MIN bytes or more, a sample decides first: a
    block whose sample doesn't compress is stored straight away, and a mode without BWT
    whose sample is clearly beaten by its BWT counterpart is not tried. ``thorough`` skips
    the sample and tries everything.
    """
    distance = 0
    if modes is not None:
        if any(m in DELTA_MODES for m in modes):
            distance = _pick_delta(data)[0]
    else:
        sample = _sample(data) if not thorough and len(data) >= PROBE_MIN else None
        if sample is not None:
            plain, with_bwt = (len(_body(sample, m, 0)) for m in (Mode.MAP, Mode.BWT_MTF_MAP))
            if min(plain, with_bwt) >= PROBE_GIVE_UP * len(sample):
                return Mode.STORED, data
        distance, worthwhile = _pick_delta(data)
        modes = tuple(m for m in Mode if m not in X86_MODES) if worthwhile else BASIC_MODES
        if _looks_like_x86(data):
            modes += X86_MODES
        if sample is not None:
            skip = {Mode.MAP} if _beaten(plain, with_bwt) else set()
            if Mode.DELTA_MAP in modes:
                delta_plain, delta_bwt = (
                    len(_body(sample, m, distance)) for m in (Mode.DELTA_MAP, Mode.DELTA_BWT_MTF_MAP)
                )
                if _beaten(delta_plain, delta_bwt):
                    skip.add(Mode.DELTA_MAP)
            modes = tuple(m for m in modes if m not in skip)
    bwts: dict[str, Bwt] = {}
    candidates = ((mode, _body(data, mode, distance, bwts)) for mode in modes)
    return min(candidates, key=lambda c: len(c[1]))  # first wins on ties


def decompress_block(mode: Mode, body: bytes, n: int) -> bytes:
    """Decode one block body of ``n`` original bytes (with the Rust core when available)."""
    if _core is not None:
        return _core.decompress_block(int(mode), bytes(body), n)
    return decompress_block_pure(mode, body, n)


def decompress_block_pure(mode: Mode, body: bytes, n: int) -> bytes:
    """The pure-Python reference of :func:`decompress_block`."""
    if mode == Mode.STORED:
        if len(body) != n:
            raise FormatError("stored block has the wrong length")
        return body
    r = Reader(body)
    distance = 0
    if mode in DELTA_MODES:
        distance = r.varint()
        if distance < 1:
            raise FormatError("invalid delta distance")
    if mode in BWT_MODES:
        primary = r.varint()
        if not 1 <= primary <= n:
            raise FormatError("invalid BWT index")
        rows = [r.varint() for _ in range(walk_count(n) - 1)]
        if any(not 1 <= row <= n for row in rows):
            raise FormatError("invalid BWT walk row")
        last = decode_map(body, r.pos, n)
        data = inverse_bwt(inverse_mtf(last) if mode in MTF_MODES else last, primary, rows)
    else:
        data = decode_map(body, r.pos, n)
    if mode in X86_MODES:
        data = x86_calls(data, forward=False)
    return inverse_delta(data, distance) if distance else data
