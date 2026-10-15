"""Gaps between sorted positions, and their split into (size class, extra bits)."""

from __future__ import annotations

import math
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence

from .arith import NUM_CLASSES

PRIOR_WEIGHT = 1024
ONE = 1 << 31  # fixed-point 1.0 (Q31)


def _compensated_sum(values: Iterable[float]) -> float:
    """sum() of floats as Python 3.12+ computes it: Neumaier-compensated, starting from 0."""
    total = c = 0.0
    for x in values:
        t = total + x
        if abs(total) >= abs(x):
            c += (total - t) + x
        else:
            c += (x - t) + total
        total = t
    if c and math.isfinite(c):
        total += c
    return total


# The cost estimates decide which series and modes the encoder picks, so they must round the
# same way on every Python version, and as the Rust core does (python_sum). sum() of floats is
# compensated since Python 3.12; the plain sum() of 3.10 and 3.11 could now and then pick
# differently.
float_sum: Callable[[Iterable[float]], float] = sum if sys.version_info >= (3, 12) else _compensated_sum


def to_gaps(positions: Iterable[int]) -> Iterator[int]:
    prev = -1
    for p in positions:
        yield p - prev - 1
        prev = p


def gap_class(gap: int) -> tuple[int, int, int]:
    """Return (class, number of extra bits, extra bits value)."""
    v = gap + 1
    if v < 4:
        return v - 1, 0, 0
    k = v.bit_length() - 1
    half = (v >> (k - 1)) & 1
    return 2 * k - 1 + half, k - 1, v & ((1 << (k - 1)) - 1)


def class_base(cls: int) -> tuple[int, int]:
    """Return (gap value with all extra bits zero, number of extra bits) for a class."""
    if cls < 3:
        return cls, 0
    k = (cls + 1) // 2
    half = cls - (2 * k - 1)
    return ((1 << k) | (half << (k - 1))) - 1, k - 1


def estimate_bytes(gaps: Iterable[int]) -> float:
    """Rough cost of coding these gaps: class entropy + extra bits, in bytes."""
    classes: Counter[int] = Counter()
    extra = 0
    for g in gaps:
        cls, nbits, _ = gap_class(g)
        classes[cls] += 1
        extra += nbits
    total = sum(classes.values())
    bits = float_sum(c * math.log2(total / c) for c in classes.values()) + extra
    return bits / 8


def position_weights(positions: Sequence[int]) -> dict[int, float]:
    """Estimated cost in bytes of each position when stored as a coded gap."""
    gaps = list(to_gaps(positions))
    info = [gap_class(g) for g in gaps]
    counts = Counter(cls for cls, _, _ in info)
    total = len(gaps)
    return {
        p: (math.log2(total / counts[cls]) + nbits) / 8
        for p, (cls, nbits, _) in zip(positions, info, strict=True)
    }


def _fixed_pow(base: int, exp: int) -> int:
    """base**exp in Q31 fixed point, by repeated squaring with truncation (integers only)."""
    result = ONE
    while exp:
        if exp & 1:
            result = (result * base) >> 31
        base = (base * base) >> 31
        exp >>= 1
    return result


def geometric_prior(n: int, k: int) -> list[int]:
    """Starting class frequencies for a symbol with k leftover positions in n bytes.

    Models the gaps as if the k positions were scattered at random (a geometric
    distribution with mean about n/k). Integer arithmetic only, so that every
    implementation computes exactly the same table.
    """
    r = ((n - k) << 31) // n if n else ONE  # probability that a byte is not this symbol
    freq = []
    for cls in range(NUM_CLASSES):
        base, nbits = class_base(cls)
        mass = _fixed_pow(r, base) - _fixed_pow(r, base + (1 << nbits))
        freq.append(1 + ((PRIOR_WEIGHT * max(mass, 0)) >> 31))
    return freq
