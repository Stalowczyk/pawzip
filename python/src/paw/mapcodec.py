"""The map body: a background byte, then one range-coded stream holding, for every
other byte value, whether it occurs, its series, and its leftover positions as gaps.

Leftover gaps are measured in *free slots* only: positions already claimed by earlier byte
values (or by this byte's own series) are skipped, because the decoder knows they are taken.

Series shapes (everything but the start) are remembered across byte values, most recent
first: a series that repeats a remembered shape is stored as its index in that list plus
its start, instead of its type and all of its parameters.
"""

from __future__ import annotations

from array import array

import numpy as np

from .arith import NUM_CLASSES, BitModel, Decoder, Encoder, Model
from .errors import FormatError
from .gaps import class_base, gap_class, geometric_prior, to_gaps
from .series import ARITH, NESTED, Series, Shape, last_position, select_series, shape_of

PARAM_COUNT = {ARITH: 2, NESTED: 4}  # parameters after the start
# Shapes remembered per body. Shape symbols 0..62 pick one and 63 announces a new shape, so
# the shape model's 64 symbols are all used.
RECENT_SHAPES = NUM_CLASSES - 1
PREDICTED_BITS = 4  # leading extra bits of a number coded with adaptive bit models

BitContexts = dict[tuple[int, int], BitModel]  # (size class, node in the bit tree) -> model


class NumberModel(Model):
    """Model for numbers: the size class, plus bit models for the leading extra bits."""

    def __init__(self, freq: list[int] | None = None, bits: BitContexts | None = None) -> None:
        super().__init__(freq)
        self.bits: BitContexts = {} if bits is None else bits


class _Models:
    """The adaptive models for metadata, shared by all symbols of one body. Each metadata
    model has its own bit contexts; the gap models of all symbols share ``gap_bits``."""

    def __init__(self) -> None:
        self.present = NumberModel()
        self.series_count = NumberModel()
        self.shape = Model()  # a symbol, not a number
        self.series_type = NumberModel()
        self.start = NumberModel()
        self.params = {(t, i): NumberModel() for t, n in PARAM_COUNT.items() for i in range(n)}
        self.leftover_count = NumberModel()
        self.gap_bits: BitContexts = {}

    def gap_model(self, free: int, count: int) -> NumberModel:
        return NumberModel(geometric_prior(free, count), self.gap_bits)


def _remember(recent: list[Shape], shape: Shape) -> None:
    """Move ``shape`` to the front of the recent-shapes list, which keeps RECENT_SHAPES."""
    if shape in recent:
        recent.remove(shape)
    recent.insert(0, shape)
    del recent[RECENT_SHAPES:]


def _bit_model(model: NumberModel, cls: int, node: int) -> BitModel:
    bit_model = model.bits.get((cls, node))
    if bit_model is None:
        bit_model = model.bits[cls, node] = BitModel()
    return bit_model


def _put(enc: Encoder, model: NumberModel, value: int) -> None:
    cls, nbits, extra = gap_class(value)
    enc.encode(model, cls)
    node = 1  # position in the tree of the bits coded so far
    for i, shift in enumerate(range(nbits - 1, -1, -1)):
        bit = (extra >> shift) & 1
        if i < PREDICTED_BITS:
            enc.encode_adaptive_bit(_bit_model(model, cls, node), bit)
            node = 2 * node + bit
        else:
            enc.encode_bit(bit)


def _get(dec: Decoder, model: NumberModel) -> int:
    cls = dec.decode(model)
    base, nbits = class_base(cls)
    extra = 0
    node = 1
    for i in range(nbits):
        if i < PREDICTED_BITS:
            bit = dec.decode_adaptive_bit(_bit_model(model, cls, node))
            node = 2 * node + bit
        else:
            bit = dec.decode_bit()
        extra = (extra << 1) | bit
    return base + extra


def _series_slices(s: Series) -> list[slice]:
    """The positions of a series as a list of slices.

    A nested series is cut along its shorter side (rows or columns), so a series with a
    million short rows costs a few slices, not a million.
    """
    if s[0] == ARITH:
        _, start, step, count = s
        return [slice(start, start + step * count, step)]
    _, start, width, stride, rows, inner = s
    if rows <= width:
        return [slice(start + r * stride, start + r * stride + inner * width, inner) for r in range(rows)]
    return [slice(start + j * inner, start + j * inner + stride * rows, stride) for j in range(width)]


def encode_map(data: bytes, bwt_output: bool = False) -> bytes:
    """The map body of ``data``. ``bwt_output`` says the data is BWT output itself (modes 5
    and 6), which changes how series are searched for (see :func:`paw.series.select_series`),
    not the format."""
    n = len(data)
    positions: list[list[int]] = [[] for _ in range(256)]
    for i, b in enumerate(data):
        positions[b].append(i)
    background = max(range(256), key=lambda b: len(positions[b]))

    enc = Encoder()
    m = _Models()
    recent: list[Shape] = []
    free = np.ones(n, dtype=bool)
    for b in range(256):
        if b == background:
            continue
        _put(enc, m.present, 1 if positions[b] else 0)
        if not positions[b]:
            continue
        series, leftovers = select_series(positions[b], frozenset(recent), bwt_output)
        _put(enc, m.series_count, len(series))
        prev_start = 0
        for s in series:
            shape = shape_of(s)
            if shape in recent:
                enc.encode(m.shape, recent.index(shape))
            else:
                enc.encode(m.shape, RECENT_SHAPES)
                _put(enc, m.series_type, s[0])
                for i, value in enumerate(s[2:]):
                    _put(enc, m.params[s[0], i], value)
            _put(enc, m.start, s[1] - prev_start)
            prev_start = s[1]
            _remember(recent, shape)
            for sl in _series_slices(s):
                free[sl] = False
        _put(enc, m.leftover_count, len(leftovers))
        if not leftovers:
            continue
        ranks = (np.cumsum(free) - 1)[leftovers].tolist()  # index among the free slots
        free_count = int(np.count_nonzero(free))
        gap_models = [m.gap_model(free_count, len(leftovers)) for _ in range(2)]
        after_zero = False  # GAP0 for the first gap and after one above 0, GAP1 after a 0
        for g in to_gaps(ranks):
            _put(enc, gap_models[after_zero], g)
            after_zero = g == 0
        free[leftovers] = False
    return bytes([background]) + enc.finish()


def _read_shape(dec: Decoder, m: _Models) -> Shape:
    kind = _get(dec, m.series_type)
    if kind not in PARAM_COUNT:
        raise FormatError(f"unknown series type {kind}")
    params = [_get(dec, m.params[kind, i]) for i in range(PARAM_COUNT[kind])]
    if kind == ARITH:
        step, count = params
        if step < 1 or count < 2:
            raise FormatError("invalid arithmetic series")
    else:
        width, stride, rows, inner = params
        if width < 2 or rows < 2 or inner < 1 or stride <= (width - 1) * inner:
            raise FormatError("invalid nested series")
    return (kind, *params)


def _read_series(dec: Decoder, m: _Models, prev_start: int, recent: list[Shape]) -> Series:
    index = dec.decode(m.shape)
    if index == RECENT_SHAPES:
        shape = _read_shape(dec, m)
    elif index < len(recent):
        shape = recent[index]
    else:
        raise FormatError("series shape index refers to no remembered shape")
    _remember(recent, shape)
    return (shape[0], prev_start + _get(dec, m.start), *shape[1:])


def decode_map(data: bytes, pos: int, n: int) -> bytes:
    if pos >= len(data):
        raise FormatError("unexpected end of data")
    background = data[pos]
    dec = Decoder(data[pos + 1 :])
    m = _Models()
    recent: list[Shape] = []
    out = np.full(n, background, dtype=np.uint8)
    free = np.ones(n, dtype=bool)
    for b in range(256):
        if b == background:
            continue
        present = _get(dec, m.present)
        if present > 1:
            raise FormatError("invalid presence flag")
        if not present:
            continue
        series_count = _get(dec, m.series_count)
        prev_start = 0
        for _ in range(series_count):
            s = _read_series(dec, m, prev_start, recent)
            prev_start = s[1]
            if last_position(s) >= n:
                raise FormatError("series outside the data")
            for sl in _series_slices(s):
                if not free[sl].all():
                    raise FormatError("series overlap positions already taken")
                free[sl] = False
                out[sl] = b

        leftovers = _get(dec, m.leftover_count)
        if not leftovers:
            if not series_count:
                raise FormatError("byte value marked present without any positions")
            continue
        slots = np.flatnonzero(free)
        if leftovers > len(slots):
            raise FormatError("more positions than free slots")
        gap_models = [m.gap_model(len(slots), leftovers) for _ in range(2)]
        after_zero = False
        ranks = array("q")  # 8 bytes per entry; a list of ints would need about 36
        r = -1
        for _ in range(leftovers):
            gap = _get(dec, gap_models[after_zero])
            r += gap + 1
            if r >= len(slots):
                raise FormatError("leftover position outside the data")
            ranks.append(r)
            after_zero = gap == 0
        taken = slots[np.frombuffer(ranks, dtype=np.int64)]
        out[taken] = b
        free[taken] = False
    return out.tobytes()
