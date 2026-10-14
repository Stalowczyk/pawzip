"""Reversible transforms applied before the position map: BWT, move-to-front, delta and the
x86 call filter."""

from __future__ import annotations

import re
from array import array
from collections.abc import Iterator, Sequence

import numpy as np

from .errors import FormatError


def _suffix_array(data: bytes) -> np.ndarray:
    """Start positions of the suffixes of ``data`` in sorted order, by prefix doubling. A
    suffix sorts before every longer one it is a prefix of (as if ended by a marker smaller
    than any byte)."""
    n = len(data)
    rank = np.frombuffer(data, dtype=np.uint8).astype(np.int64)
    k = 1
    while True:
        second = np.full(n, -1, dtype=np.int64)  # -1: past the end
        second[: max(0, n - k)] = rank[k:]
        order = np.lexsort((second, rank))
        r1, r2 = rank[order], second[order]
        changed = (r1[1:] != r1[:-1]) | (r2[1:] != r2[:-1])
        new = np.empty(n, dtype=np.int64)
        new[order] = np.concatenate(([0], np.cumsum(changed)))
        rank = new
        if rank[order[-1]] == n - 1:  # all suffixes told apart
            return order
        k *= 2


def bwt_with_rows(data: bytes) -> tuple[bytes, int, list[int]]:
    """Burrows-Wheeler transform with an end marker. Returns (last column, primary index,
    walk rows).

    The input gets a marker smaller than every byte, and its n + 1 suffixes are sorted. Row
    r's last-column entry is the symbol before suffix r. Row 0 is the marker alone, so its
    entry is the input's last byte. The marker's own entry (in the row of the whole input,
    the primary index, 1 ≤ primary ≤ n) is left out, which leaves n bytes. Walk row j is the
    row of the suffix starting at j * L (see walk_count).
    """
    n = len(data)
    if n == 0:
        return b"", 0, []
    src = np.frombuffer(data, dtype=np.uint8)
    sa = _suffix_array(data)
    at = int(np.flatnonzero(sa == 0)[0])
    before = np.delete(src[sa - 1], at)  # the marker's entry is not stored
    last = bytes([data[-1]]) + before.tobytes()
    k = walk_count(n)
    length = -(-n // k)
    rows = [int(np.flatnonzero(sa == j * length)[0]) + 1 for j in range(1, k)]
    return last, at + 1, rows


def bwt(data: bytes) -> tuple[bytes, int]:
    """Burrows-Wheeler transform: (last column, primary index); see :func:`bwt_with_rows`."""
    last, primary, _ = bwt_with_rows(data)
    return last, primary


# Stored walk rows: one per WALK_UNIT bytes of a BWT block, up to MAX_WALKS. They tell a
# decoder where the inverse walk stands at evenly spaced positions, so it can run several
# independent walks at once (the walk itself is a long chain of dependent memory reads).
WALK_UNIT = 1 << 17
MAX_WALKS = 16


def walk_count(n: int) -> int:
    """Number of walks K for a BWT block of n bytes (K - 1 rows are stored)."""
    return max(1, min(MAX_WALKS, n // WALK_UNIT))


def _walk_table(last: bytes, primary: int) -> tuple[array[int], bytes]:
    """The walk over the n + 1 rows: row r holds the suffix starting with byte first[r] and
    continues at row nxt[r]. Row 0 (the marker) continues at the primary row.

    The rows are a stable counting sort of the last column, with the marker's entry put back
    at the primary row. An unsigned 32-bit array holds every row of a block (< 4 GiB) in 4
    bytes per entry; a list of ints would need about 36.
    """
    src = np.frombuffer(last, dtype=np.uint8)
    order = np.argsort(src, kind="stable").astype(np.uintc)
    nxt = np.empty(len(last) + 1, dtype=np.uintc)
    nxt[0] = primary
    nxt[1:] = order
    nxt[1:][order >= primary] += 1  # rows after the marker's entry move down one
    first = np.concatenate(([0], src[order])).astype(np.uint8)
    return array("I", nxt.tobytes()), first.tobytes()


def walk_rows(last: bytes, primary: int) -> list[int]:
    """The rows the inverse walk stands at just before it produces positions L, 2L, ...,
    (K-1)L, where K = walk_count(n) and L = ceil(n / K), found by walking."""
    n = len(last)
    k = walk_count(n)
    if k == 1:
        return []
    length = -(-n // k)
    nxt, _ = _walk_table(last, primary)
    rows = []
    row = primary
    for i in range(1, k * length - length + 1):
        row = nxt[row]
        if i % length == 0:
            rows.append(row)
    return rows


def inverse_bwt(last: bytes, primary: int, rows: Sequence[int] = ()) -> bytes:
    """Undo the BWT (1 ≤ primary ≤ n); ``rows`` (from bwt_with_rows) are checked against
    the walk."""
    n = len(last)
    if n == 0:
        return b""
    nxt, first = _walk_table(last, primary)
    expected = {(j + 1) * -(-n // walk_count(n)): r for j, r in enumerate(rows)}
    out = bytearray(n)
    row = primary
    for i in range(n):
        if i in expected and expected[i] != row:
            raise FormatError("stored BWT walk row does not match the data")
        out[i] = first[row]
        row = nxt[row]
    return bytes(out)


def mtf(data: bytes) -> bytes:
    table = list(range(256))
    out = bytearray(len(data))
    for i, b in enumerate(data):
        j = table.index(b)
        out[i] = j
        if j:
            del table[j]
            table.insert(0, b)
    return bytes(out)


def inverse_mtf(data: bytes) -> bytes:
    table = list(range(256))
    out = bytearray(len(data))
    for i, j in enumerate(data):
        b = table[j]
        out[i] = b
        if j:
            del table[j]
            table.insert(0, b)
    return bytes(out)


def delta(data: bytes, distance: int) -> bytes:
    """Replace each byte by its difference (mod 256) from the byte `distance` earlier."""
    src = np.frombuffer(data, dtype=np.uint8)
    out = src.copy()
    out[distance:] = src[distance:] - src[:-distance]
    return out.tobytes()


_CALL = re.compile(rb"[\xe8\xe9]")


def x86_call_sites(data: bytes) -> Iterator[int]:
    """The positions of the E8 and E9 bytes whose address the x86 call filter converts. The
    filter and its inverse find the same ones, since it changes only the address bytes."""
    resume = 0  # the 4 address bytes after an E8 or E9 are never scanned themselves
    for m in _CALL.finditer(data):
        i = m.start()
        if i < resume:
            continue
        if i + 5 > len(data):
            return
        resume = i + 5
        if data[i + 4] in (0x00, 0xFF):
            yield i


def x86_calls(data: bytes, forward: bool) -> bytes:
    """The x86 call filter (spec: "x86 calls"): the 32-bit relative address after each CALL
    (E8) or JMP (E9) whose top byte is 00 or FF becomes absolute (``forward``) or relative
    again, so that calls to the same place look alike."""
    out = bytearray(data)
    for i in x86_call_sites(data):
        v = int.from_bytes(data[i + 1 : i + 5], "little")
        v = (v + i + 5 if forward else v - i - 5) & 0xFFFFFFFF
        v = v & 0x01FFFFFF | (0xFE000000 if v & 0x01000000 else 0)  # top byte stays 00 or FF
        out[i + 1 : i + 5] = v.to_bytes(4, "little")
    return bytes(out)


def inverse_delta(data: bytes, distance: int) -> bytes:
    """Undo :func:`delta`, with at most about sqrt(n) numpy operations for any distance."""
    out = np.frombuffer(data, dtype=np.uint8).copy()
    n = len(out)
    if distance * distance <= n:  # few residue classes: a running sum along each one
        for r in range(distance):
            out[r::distance] = np.cumsum(out[r::distance], dtype=np.uint8)
    else:  # few chunks of `distance` bytes: add the already restored chunk before each
        for start in range(distance, n, distance):
            end = min(start + distance, n)
            out[start:end] += out[start - distance : end - distance]
    return out.tobytes()
