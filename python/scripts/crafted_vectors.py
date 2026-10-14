"""Hand-made vectors: spec/vectors/unusual/ and spec/vectors/invalid/.

Vector files are frozen with format v1: new ones may be added, existing ones are
never changed or deleted (``write_frozen``).

``unusual/`` holds valid files that the reference encoder never writes, but another encoder
may: a stored extension, optional header data, non-minimal varints, a delta distance past
the end, padded streams, a block shorter than B in the middle, a "new" shape that is already
in the shape list. Each ``<input>.<case>.paw`` must decode to ``<input>.bin``.

``invalid/`` holds one file per rule of FORMAT.md's "Validation" section. Each must be
rejected with an error (never a crash or a hang). Apart from the broken rule, every file is
as valid as possible, including its CRC where the data is known.

Both folders are written by ``make_vectors.py``; every case is checked against both
decoders here before it is written.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path

from paw import FormatError, Mode, blocks, decompress
from paw.arith import Encoder
from paw.mapcodec import RECENT_SHAPES, _Models, _put, encode_map
from paw.series import ARITH, NESTED
from paw.transforms import bwt_with_rows, delta
from paw.varint import Reader, write_varint

SHORT = b"hello paw, hello map!\n" * 2  # 44 bytes
LONG = b"a" * 262_144  # 256 KiB: BWT bodies store one walk row
MAX_NUMBER = 3 * 2**31 - 2  # class 63 with every extra bit set


def varint(v: int) -> bytes:
    out = bytearray()
    write_varint(out, v)
    return bytes(out)


def padded(v: int, extra: int = 2) -> bytes:
    """A non-minimal varint: ``extra`` more bytes than needed (still at most 10)."""
    raw = bytearray(varint(v))
    raw[-1] |= 0x80
    return bytes(raw) + b"\x80" * (extra - 1) + b"\x00"


def field(v: int | bytes) -> bytes:
    """An int becomes a minimal varint; bytes are written as they are."""
    return v if isinstance(v, bytes) else varint(v)


def block(
    data: bytes,
    mode: int,
    body: bytes,
    *,
    n: int | bytes | None = None,
    crc: int | None = None,
    body_size: int | bytes | None = None,
) -> bytes:
    crc = zlib.crc32(data) if crc is None else crc
    return (
        field(len(data) if n is None else n)
        + bytes([mode])
        + crc.to_bytes(4, "little")
        + field(len(body) if body_size is None else body_size)
        + body
    )


def paw_file(
    blocks_: list[bytes],
    total: int | bytes,
    *,
    block_size: int | bytes = 1 << 24,
    ext: bytes = b"",
    extra: bytes = b"",
    extra_len: int | bytes | None = None,
    end: bytes = b"\x00",
    magic: bytes = b"PAW",
    version: int = 1,
) -> bytes:
    header = magic + bytes([version]) + field(block_size) + bytes([len(ext)]) + ext
    header += field(len(extra) if extra_len is None else extra_len) + extra
    return header + b"".join(blocks_) + end + field(total)


def one_block(data: bytes, mode: int, body: bytes, **kw) -> bytes:
    """A file holding ``data`` as one block with this body."""
    return paw_file([block(data, mode, body, **kw)], len(data))


def bwt_body(
    data: bytes, *, primary: int | bytes | None = None, rows: list[int | bytes] | None = None
) -> bytes:
    """A mode 5 body (BWT without MTF), with the primary index or walk rows replaced."""
    last, p, r = bwt_with_rows(data)
    head = field(p if primary is None else primary) + b"".join(
        field(x) for x in (r if rows is None else rows)
    )
    return head + encode_map(last, bwt_output=True)


class MapWriter:
    """Writes a map body number by number, so that any field can hold any value.

    ``value(b)`` marks the byte values before ``b`` absent and starts ``b`` (its presence
    flag comes next); ``finish`` marks the rest absent. The caller tracks the shape list.
    """

    def __init__(self, background: int = 0) -> None:
        self.enc = Encoder()
        self.m = _Models()
        self.background = background
        self.next = 0

    def _absent_until(self, b: int) -> None:
        for v in range(self.next, b):
            if v != self.background:
                _put(self.enc, self.m.present, 0)
        self.next = b

    def value(self, b: int, present: int = 1) -> MapWriter:
        self._absent_until(b)
        _put(self.enc, self.m.present, present)
        self.next = b + 1
        return self

    def series_count(self, k: int) -> MapWriter:
        _put(self.enc, self.m.series_count, k)
        return self

    def new_shape(self, kind: int, params: list[int], start: int | None) -> MapWriter:
        self.enc.encode(self.m.shape, RECENT_SHAPES)
        _put(self.enc, self.m.series_type, kind)
        for i, p in enumerate(params):
            _put(self.enc, self.m.params[kind, i], p)
        if start is not None:
            _put(self.enc, self.m.start, start)
        return self

    def old_shape(self, index: int, start: int | None) -> MapWriter:
        self.enc.encode(self.m.shape, index)
        if start is not None:
            _put(self.enc, self.m.start, start)
        return self

    def leftovers(self, free: int, gaps: list[int], count: int | None = None) -> MapWriter:
        count = len(gaps) if count is None else count
        _put(self.enc, self.m.leftover_count, count)
        if count:
            models = [self.m.gap_model(free, count) for _ in range(2)]
            after_zero = False
            for g in gaps:
                _put(self.enc, models[after_zero], g)
                after_zero = g == 0
        return self

    def finish(self, complete: bool = True) -> bytes:
        if complete:
            self._absent_until(256)
        return bytes([self.background]) + self.enc.finish()


# --- valid files the reference encoder doesn't write ------------------------------------

SHAPES = bytes([1, 1, 1, 2, 2, 2, 1, 0, 0, 2, 0, 0])


def repeated_shape_body(second_index: int) -> bytes:
    """Byte 1: new shape A (step 2) at 0, new shape B (step 5) at 1 -> list [B, A].
    Byte 2: shape A sent again as new (it moves to the front: [A, B]), then list entry
    ``second_index`` at 4 (1 = B is valid; 2 doesn't exist)."""
    w = MapWriter()
    w.value(1).series_count(2).new_shape(ARITH, [2, 2], 0).new_shape(ARITH, [5, 2], 1).leftovers(0, [])
    w.value(2).series_count(2).new_shape(ARITH, [2, 2], 3).old_shape(second_index, 1).leftovers(0, [])
    return w.finish()


def padded_map(body: bytes, extra_coded: int, extra_raw: int) -> bytes:
    """A map body with zero bytes added to the range-coded part (R grows) and the raw bits."""
    r = Reader(body, 1)
    size = r.varint()
    coded, raw = body[r.pos : r.pos + size], body[r.pos + size :]
    return body[:1] + varint(size + extra_coded) + coded + bytes(extra_coded) + raw + bytes(extra_raw)


def nonminimal_map(body: bytes) -> bytes:
    """A map body whose range-coded size R is a non-minimal varint."""
    r = Reader(body, 1)
    size = r.varint()
    return body[:1] + padded(size) + body[r.pos :]


def unusual() -> dict[str, tuple[bytes, bytes, str]]:
    """name -> (input, .paw file, what is unusual)."""
    n = len(SHORT)
    map_body = encode_map(SHORT)
    last, primary, _ = bwt_with_rows(delta(SHORT, 3))
    delta_bwt = padded(3) + padded(primary) + nonminimal_map(encode_map(last, bwt_output=True))
    return {
        "short.extension": (
            SHORT,
            paw_file([block(SHORT, 1, map_body)], n, ext=b"txt"),
            "stored extension 'txt'",
        ),
        "short.header-extra": (
            SHORT,
            paw_file([block(SHORT, 1, map_body)], n, extra=b"\x01later\xff"),
            "7 bytes of optional header data, which decoders skip",
        ),
        "short.nonminimal-container": (
            SHORT,
            paw_file(
                [block(SHORT, 1, map_body, n=padded(n), body_size=padded(len(map_body)))],
                padded(n, 8),
                block_size=padded(1 << 24),
                extra_len=padded(0),
                end=b"\x80\x00",
            ),
            "every container varint non-minimal (block size, extra length, n, body size, end record, total)",
        ),
        "short.nonminimal-body": (
            SHORT,
            one_block(SHORT, 6, delta_bwt),
            "mode 6 with a non-minimal delta distance, primary index and range-coded size",
        ),
        "short.delta-past-end": (
            SHORT,
            one_block(SHORT, 3, varint(n + 5) + map_body),
            "mode 3 with a delta distance above n (the block is unchanged)",
        ),
        "short.delta-equal-n": (
            SHORT,
            one_block(SHORT, 6, varint(n) + bwt_body(SHORT)),
            "mode 6 with a delta distance of exactly n",
        ),
        "short.padded-streams": (
            SHORT,
            one_block(SHORT, 1, padded_map(map_body, 3, 5)),
            "zero bytes after the range-coded part (counted in R) and after the raw bits",
        ),
        "short.short-middle-block": (
            SHORT,
            paw_file(
                [block(SHORT[:10], 1, encode_map(SHORT[:10])), block(SHORT[10:], 0, SHORT[10:])],
                n,
                block_size=n - 10,
            ),
            "a first block shorter than the block size B, then a full one",
        ),
        "shapes.repeated-new-shape": (
            SHAPES,
            one_block(SHAPES, 1, repeated_shape_body(1)),
            "a shape sent as new (symbol 63) while it is already in the shape list",
        ),
    }


# --- invalid files ----------------------------------------------------------------------


def map_case(writer: MapWriter, n: int = 12, complete: bool = True) -> bytes:
    return one_block(bytes(n), 1, writer.finish(complete))


def invalid() -> dict[str, tuple[str, bytes]]:
    """name -> (the rule it breaks, .paw file)."""
    n = len(SHORT)
    good = block(SHORT, 1, encode_map(SHORT))
    ok = paw_file([good], n)
    header = len(ok) - len(good) - 2  # the end record is 2 bytes here
    _, _, rows = bwt_with_rows(LONG)
    assert len(rows) == 1
    long_n = len(LONG)
    nested = {
        "nested-width-1": [1, 5, 2, 1],
        "nested-rows-1": [2, 5, 1, 1],
        "nested-inner-0": [2, 5, 2, 0],
        "nested-stride-too-small": [2, 1, 2, 1],
    }
    cases = {
        # container
        "bad-magic": ("container: the magic is not PAW", b"PAX" + ok[3:]),
        "unknown-version": ("container: unknown version", ok[:3] + b"\x02" + ok[4:]),
        "varint-too-long": (
            "container: a varint longer than 10 bytes",
            paw_file([good], n, block_size=b"\x80" * 10 + b"\x01"),
        ),
        "varint-too-large": (
            "container: a varint not below 2^64",
            paw_file([good], n, block_size=b"\xff" * 9 + b"\x02"),
        ),
        "block-size-zero": ("container: B is 0", paw_file([good], n, block_size=0)),
        "block-size-too-large": ("container: B is 2^32 or more", paw_file([good], n, block_size=1 << 32)),
        "extension-too-long": ("container: L above 16", paw_file([good], n, ext=b"a" * 17)),
        "extension-bad-character": (
            "container: a character not allowed in the extension",
            paw_file([good], n, ext=b"../x"),
        ),
        "extra-too-long": ("container: X above 65536", paw_file([good], n, extra_len=65537)),
        "block-larger-than-b": (
            "container: a block holds more than B bytes",
            paw_file([good], n, block_size=n - 1),
        ),
        "unknown-mode": ("container: a mode that is not 0-7", one_block(SHORT, 8, SHORT)),
        "truncated-header": ("container: the data ends inside a block header", ok[: header + 3]),
        "truncated-body": ("container: the data ends inside a body", ok[: len(ok) - 3]),
        "crc-mismatch": (
            "container: the CRC-32 doesn't match",
            one_block(SHORT, 1, encode_map(SHORT), crc=zlib.crc32(SHORT) ^ 1),
        ),
        "total-mismatch": (
            "container: the end record's total differs from the sum of the blocks",
            paw_file([good], n + 1),
        ),
        "trailing-data": ("container: something follows the end record", ok + b"\x00"),
        # bodies
        "stored-wrong-length": (
            "body: a stored body isn't n bytes long",
            one_block(SHORT, 0, SHORT[:-1], n=n),
        ),
        "delta-distance-zero": (
            "body: a delta distance of 0",
            one_block(SHORT, 3, varint(0) + encode_map(SHORT)),
        ),
        "primary-zero": ("body: a primary index of 0", one_block(SHORT, 5, bwt_body(SHORT, primary=0))),
        "primary-too-large": (
            "body: a primary index above n",
            one_block(SHORT, 5, bwt_body(SHORT, primary=n + 1)),
        ),
        "walk-row-zero": ("body: a walk row of 0", one_block(LONG, 5, bwt_body(LONG, rows=[0]))),
        "walk-row-too-large": (
            "body: a walk row above n",
            one_block(LONG, 5, bwt_body(LONG, rows=[long_n + 1])),
        ),
        "walk-row-mismatch": (
            "body: a walk row that doesn't match the walk",
            one_block(LONG, 5, bwt_body(LONG, rows=[rows[0] % long_n + 1])),
        ),
        "map-body-empty": ("body: an empty map body (no background byte)", one_block(SHORT, 1, b"")),
        "range-size-too-large": (
            "body: the range-coded size R exceeds the bytes left",
            one_block(SHORT, 1, b"\x00" + varint(100) + b"\x12\x34"),
        ),
        # map body
        "presence-flag-2": (
            "map body: a presence flag that is neither 0 nor 1",
            map_case(MapWriter().value(1, present=2), complete=False),
        ),
        "present-without-positions": (
            "map body: a present byte value with no series and no leftovers",
            map_case(MapWriter().value(1).series_count(0).leftovers(12, [])),
        ),
        "shape-index-empty-list": (
            "map body: a shape symbol naming an entry of an empty list",
            map_case(MapWriter().value(1).series_count(1).old_shape(0, 0), complete=False),
        ),
        "shape-index-after-repeat": (
            "map body: a shape symbol naming a missing entry (a repeated new shape moves, it isn't added)",
            one_block(SHAPES, 1, repeated_shape_body(2)),
        ),
        "series-unknown-type": (
            "map body: a series type that is not 0 or 1",
            map_case(MapWriter().value(1).series_count(1).new_shape(2, [], None), complete=False),
        ),
        "arith-step-0": (
            "map body: an arithmetic series with step 0",
            map_case(MapWriter().value(1).series_count(1).new_shape(ARITH, [0, 2], 0), complete=False),
        ),
        "arith-count-1": (
            "map body: an arithmetic series with count 1",
            map_case(MapWriter().value(1).series_count(1).new_shape(ARITH, [1, 1], 0), complete=False),
        ),
        "series-past-end": (
            "map body: a series reaching position n",
            map_case(MapWriter().value(1).series_count(1).new_shape(ARITH, [5, 3], 3), complete=False),
        ),
        "series-position-overflow": (
            "map body: a series whose last position overflows 64 bits",
            map_case(
                MapWriter().value(1).series_count(1).new_shape(ARITH, [MAX_NUMBER, MAX_NUMBER], 0),
                complete=False,
            ),
        ),
        "series-overlap-same-value": (
            "map body: a series touching a position of an earlier series of the same byte value",
            map_case(
                MapWriter().value(1).series_count(2).new_shape(ARITH, [2, 2], 0).old_shape(0, 2),
                complete=False,
            ),
        ),
        "series-overlap-earlier-value": (
            "map body: a series touching a position taken by an earlier byte value",
            map_case(
                MapWriter()
                .value(1)
                .series_count(1)
                .new_shape(ARITH, [2, 2], 0)
                .leftovers(0, [])
                .value(2)
                .series_count(1)
                .old_shape(0, 2),
                complete=False,
            ),
        ),
        "leftover-count-too-large": (
            "map body: a leftover count above the number of free slots",
            map_case(MapWriter().value(1).series_count(0).leftovers(12, [], count=13), complete=False),
        ),
        "leftover-past-end": (
            "map body: a leftover rank past the last free slot",
            map_case(MapWriter().value(1).series_count(0).leftovers(12, [5, 7])),
        ),
    }
    for name, params in nested.items():
        rule = f"map body: a nested series breaking its constraints ({name.removeprefix('nested-')})"
        w = MapWriter().value(1).series_count(1).new_shape(NESTED, params, 0)
        cases[name] = (rule, map_case(w, complete=False))
    return cases


def _decoders():
    """decompress with the Rust core (when built) and with the pure-Python decoder."""
    native = blocks._core

    def pure(blob: bytes) -> bytes:
        blocks._core = None
        try:
            return decompress(blob)
        finally:
            blocks._core = native

    return [decompress, pure] if native is not None else [pure]


class FrozenVectorError(Exception):
    pass


def write_frozen(path: Path, content: bytes) -> None:
    """Write a new vector file. Format v1 is frozen: an existing vector must never
    change, so different bytes for an existing file are an error, not an update."""
    if path.exists():
        if path.read_bytes() != content:
            raise FrozenVectorError(
                f"{path.name} would change. Vector files are frozen with format v1: if the encoder "
                "changed on purpose, its byte-for-byte test needs a new set of files"
            )
        return
    path.write_bytes(content)


def write(vectors: Path) -> int:
    count = 0
    for sub in ("unusual", "invalid"):
        (vectors / sub).mkdir(exist_ok=True)

    folder = vectors / "unusual"
    manifest = []
    inputs: dict[str, bytes] = {}
    for name, (data, blob, note) in unusual().items():
        for dec in _decoders():
            assert dec(blob) == data, name
        source = name.split(".")[0]
        inputs[source] = data
        write_frozen(folder / f"{name}.paw", blob)
        manifest.append(
            {
                "paw": f"{name}.paw",
                "input": f"{source}.bin",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "note": note,
            }
        )
    for source, data in inputs.items():
        write_frozen(folder / f"{source}.bin", data)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", newline="\n")
    count += len(manifest)

    folder = vectors / "invalid"
    manifest = []
    for name, (rule, blob) in invalid().items():
        for dec in _decoders():
            try:
                dec(blob)
            except FormatError:
                pass
            else:
                raise AssertionError(f"{name} was accepted")
        write_frozen(folder / f"{name}.paw", blob)
        manifest.append({"paw": f"{name}.paw", "rule": rule})
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", newline="\n")
    return count + len(manifest)


assert Mode.BWT_MAP == 5 and Mode.DELTA_BWT_MAP == 6  # the mode numbers used above
