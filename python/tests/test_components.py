"""Unit tests for the building blocks, with hand-checked expected values."""

import random
import tracemalloc

import pytest

from paw.arith import BIT_ONE, NUM_CLASSES, BitModel, Decoder, Encoder, Model
from paw.gaps import _compensated_sum, class_base, float_sum, gap_class, geometric_prior, to_gaps
from paw.mapcodec import RECENT_SHAPES, NumberModel, _Models, _put, _series_slices, decode_map, encode_map
from paw.series import ARITH, NESTED, reused_cost, select_series, series_cost, series_positions
from paw.transforms import bwt, delta, inverse_bwt, inverse_delta, mtf
from paw.varint import FormatError, Reader, varint_len, write_varint

# --- varint ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "encoded"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (16384, b"\x80\x80\x01"),
    ],
)
def test_varint_known_encodings(n, encoded):
    out = bytearray()
    write_varint(out, n)
    assert bytes(out) == encoded and varint_len(n) == len(encoded)


def test_varint_errors():
    with pytest.raises(ValueError):
        write_varint(bytearray(), -1)
    with pytest.raises(ValueError):
        write_varint(bytearray(), 2**64)
    with pytest.raises(FormatError):
        Reader(b"\x80\x80").varint()  # unterminated
    with pytest.raises(FormatError, match="longer than 10 bytes"):
        Reader(b"\x80" * 10 + b"\x00").varint()  # even if the value is small
    with pytest.raises(FormatError, match="larger than 64 bits"):
        Reader(b"\xff" * 9 + b"\x02").varint()  # 10 bytes, but bit 64 is set


def test_varint_limits_and_non_minimal_encodings():
    assert Reader(b"\xff" * 9 + b"\x01").varint() == 2**64 - 1  # the largest value
    assert Reader(b"\x80\x00").varint() == 0  # non-minimal encodings are allowed
    assert Reader(b"\x81\x80\x80\x00").varint() == 1


# --- map body validation ----------------------------------------------------------------


def coded_map_body(background, numbers):
    """A map body whose coded stream holds ``numbers``: (model, value) pairs. The model is a
    metadata model name, ("param", type, index), or "shape" (a symbol, coded directly)."""
    enc, models = Encoder(), _Models()
    for name, value in numbers:
        if name == "shape":
            enc.encode(models.shape, value)
        elif isinstance(name, tuple):
            _put(enc, models.params[name[1], name[2]], value)
        else:
            _put(enc, getattr(models, name), value)
    return bytes([background]) + enc.finish()


@pytest.mark.parametrize(
    ("numbers", "message"),
    [
        ([("present", 2)], "invalid presence flag"),
        ([("present", 1), ("series_count", 0), ("leftover_count", 0)], "without any positions"),
        ([("present", 1), ("series_count", 0), ("leftover_count", 11)], "more positions than free"),
        ([("present", 1), ("series_count", 1), ("shape", 0)], "no remembered shape"),
        ([("present", 1), ("series_count", 1), ("shape", 1)], "no remembered shape"),
    ],
    ids=["flag above 1", "present but empty", "too many leftovers", "shape 0 of none", "shape 1 of none"],
)
def test_map_decoder_rejects(numbers, message):
    # Byte value 0 is the background, so byte value 1 is the first one coded.
    with pytest.raises(FormatError, match=message):
        decode_map(coded_map_body(0, numbers), 0, 10)


def test_map_decoder_reuses_a_remembered_shape():
    first = [("present", 1), ("series_count", 1), ("shape", RECENT_SHAPES)]  # byte value 1: new shape
    first += [("series_type", ARITH), (("param", ARITH, 0), 3), (("param", ARITH, 1), 2), ("start", 0)]
    second = [("present", 1), ("series_count", 1), ("shape", 0), ("start", 1)]  # byte value 2: reuse
    rest = [("present", 0)] * 253  # byte values 3-255
    body = coded_map_body(0, [*first, ("leftover_count", 0), *second, ("leftover_count", 0), *rest])
    assert decode_map(body, 0, 6) == bytes([1, 2, 0, 1, 2, 0])


def test_remembered_shapes_make_repeated_rectangles_cheaper(monkeypatch):
    # Three byte values, each an 8-wide, 20-row rectangle in a different column: one shape.
    image = bytearray(b"." * 64 * 20)
    for value, column in ((ord("a"), 4), (ord("b"), 24), (ord("c"), 44)):
        for row in range(20):
            image[row * 64 + column : row * 64 + column + 8] = bytes([value]) * 8
    data = bytes(image)
    remembered = encode_map(data)
    assert decode_map(remembered, 0, len(data)) == data
    monkeypatch.setattr("paw.mapcodec.RECENT_SHAPES", 0)  # remember nothing: every shape is new
    forgotten = encode_map(data)
    assert decode_map(forgotten, 0, len(data)) == data
    assert len(remembered) < len(forgotten)


def test_a_known_shape_makes_a_short_series_worthwhile():
    positions = [100, 340, 580]
    assert select_series(positions) == ([], positions)  # too short to pay for a new shape
    assert select_series(positions, frozenset({(ARITH, 240, 3)})) == ([(ARITH, 100, 240, 3)], [])


# --- gaps -----------------------------------------------------------------------------


def test_to_gaps():
    assert list(to_gaps([0, 1, 5, 6, 20])) == [0, 0, 3, 0, 13]


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (0, (0, 0, 0)),
        (2, (2, 0, 0)),
        (3, (3, 1, 0)),
        (4, (3, 1, 1)),
        (5, (4, 1, 0)),
        (7, (5, 2, 0)),
        (14, (6, 2, 3)),
    ],
)
def test_gap_class_known_values(gap, expected):
    assert gap_class(gap) == expected
    assert class_base(expected[0])[0] + expected[2] == gap


def test_geometric_prior_shape():
    freq = geometric_prior(4000, 100)  # mean gap about 40
    assert len(freq) == NUM_CLASSES and min(freq) >= 1
    peak = max(range(NUM_CLASSES), key=freq.__getitem__)
    lo, _ = class_base(peak)
    assert 8 <= lo <= 64
    assert sum(freq) <= NUM_CLASSES + 1024


def test_geometric_prior_edge_cases():
    assert geometric_prior(10, 0) == [1] * NUM_CLASSES  # nothing to place
    full = geometric_prior(5, 5)  # every slot is this symbol: gap 0 is certain
    assert full[0] == 1025 and full[1:] == [1] * (NUM_CLASSES - 1)
    assert geometric_prior(0, 0) == [1] * NUM_CLASSES


def test_geometric_prior_known_table():
    # Pinned values: other implementations must reproduce this table exactly.
    assert geometric_prior(1000, 10)[:12] == [11, 11, 11, 20, 20, 38, 37, 69, 63, 112, 95, 150]


def test_float_sum_is_compensated_on_every_version():
    # Plain left-to-right addition loses the 1.0 next to 1e16; Python 3.12+ and the Rust core keep it.
    assert float_sum([1e16, 1.0, -1e16]) == _compensated_sum([1e16, 1.0, -1e16]) == 1.0
    assert _compensated_sum([]) == 0.0


# --- range coder --------------------------------------------------------------------------


def test_model_rescales_and_stays_valid():
    model = Model()
    for _ in range(5000):
        model.update(3)
    assert model.total == sum(model.freq) <= 16384 and min(model.freq) >= 1


def test_bit_model_never_reaches_certainty():
    model = BitModel()
    for _ in range(10_000):
        model.update(0)
    assert BIT_ONE // 2 < model.p < BIT_ONE  # a 1 must stay codable
    for _ in range(10_000):
        model.update(1)
    assert 0 < model.p < BIT_ONE // 2  # and so must a 0


def test_predictable_extra_bits_cost_little():
    # gaps of 1000 or 1001 always share their first extra bits: once learned, they are cheap
    enc, model = Encoder(), NumberModel()
    for i in range(2000):
        _put(enc, model, 1000 + i % 2)
    raw = 2000 * (1 + 8)  # if every extra bit cost a full bit (class ~1 bit when learned)
    assert len(enc.finish()) * 8 < raw * 0.6


def test_skewed_stream_compresses_well():
    enc, model = Encoder(), Model()
    for _ in range(10000):
        enc.encode(model, 0)
    data = enc.finish()
    assert len(data) < 40
    dec, model = Decoder(data), Model()
    assert all(dec.decode(model) == 0 for _ in range(10000))


def test_carries_and_raw_bits_round_trip():
    # Long stretches of near-certain bits keep low just under a byte boundary, so carries
    # have to ripple back through waiting 0xFF bytes; raw bits go to their own stream.
    rng = random.Random(11)
    script = [(rng.random() < 0.002, rng.getrandbits(1)) for _ in range(200_000)]
    enc, bit_model = Encoder(), BitModel()
    for rare, raw in script:
        enc.encode_adaptive_bit(bit_model, int(rare))
        enc.encode_bit(raw)
    dec, bit_model = Decoder(enc.finish()), BitModel()
    for rare, raw in script:
        assert dec.decode_adaptive_bit(bit_model) == rare
        assert dec.decode_bit() == raw


def test_coded_stream_layout():
    enc = Encoder()
    for bit in (1, 0, 1):
        enc.encode_bit(bit)
    assert enc.finish() == bytes([0, 0b10100000])  # no range-coded bytes, then the raw bits
    with pytest.raises(FormatError, match="range-coded part longer"):
        Decoder(bytes([5, 1, 2]))


def test_decoder_rejects_impossible_model_count():
    with pytest.raises(FormatError):
        Model().find(10**9)


# --- transforms -----------------------------------------------------------------------


def test_banana_bwt():
    # sorted suffixes of "banana$": $, a$, ana$, anana$, banana$, na$, nana$; the symbols
    # before them: a n n b $ a a. The marker's entry (row 4, the whole input) is left out.
    assert bwt(b"banana") == (b"annbaa", 4)
    assert inverse_bwt(b"annbaa", 4) == b"banana"
    assert bwt(b"aaa") == (b"aaa", 3)  # $, a$, aa$, aaa$


def test_inverse_bwt_memory_stays_compact():
    # The row table must stay a compact array: a list of Python ints would take about 48x the
    # block in memory.
    rng = random.Random(7)
    data = bytes(rng.choice(b"abcdefgh ") for _ in range(1 << 18))
    last, primary = bwt(data)
    tracemalloc.start()
    try:
        assert inverse_bwt(last, primary) == data
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 25 * len(data)


def test_mtf_known_values():
    assert mtf(b"aaab") == bytes([97, 0, 0, 98])
    assert mtf(b"abab") == bytes([97, 98, 1, 1])


def test_delta_known_values():
    assert delta(bytes([10, 12, 15, 15, 0]), 1) == bytes([10, 2, 3, 0, 241])
    assert inverse_delta(bytes([10, 2, 3, 0, 241]), 1) == bytes([10, 12, 15, 15, 0])
    assert delta(b"ab", 5) == b"ab"


@pytest.mark.parametrize("distance", [1, 3, 31, 32, 33, 500, 999, 1000, 1001, 10**12])
def test_inverse_delta_any_distance(distance):
    # both strategies: running sums for small distances, chunk by chunk for large ones
    data = random.Random(distance).randbytes(1000)
    assert inverse_delta(delta(data, distance), distance) == data


# --- series ---------------------------------------------------------------------------


def test_series_positions_and_cost():
    assert list(series_positions((ARITH, 4, 5, 3))) == [4, 9, 14]
    assert series_positions((NESTED, 0, 2, 10, 3, 3)) == [0, 3, 10, 13, 20, 23]
    # start 4, step 5 and count 3 each cost ~3 class bits + 1 extra bit; plus 1 bit for the type
    assert series_cost((ARITH, 4, 5, 3)) == (1 + 4 + 4 + 4) / 8
    assert reused_cost((ARITH, 4, 5, 3)) == (2 + 4) / 8  # a remembered shape: index + start


@pytest.mark.parametrize(
    "s",
    [(NESTED, 3, 2, 6, 1000, 1), (NESTED, 0, 50, 200, 3, 2), (NESTED, 7, 4, 9, 4, 2), (ARITH, 5, 7, 40)],
    ids=["tall", "wide", "square", "arithmetic"],
)
def test_series_slices_cover_exactly_the_positions(s):
    slices = _series_slices(s)
    covered = sorted(p for sl in slices for p in range(sl.start, sl.stop, sl.step))
    assert covered == sorted(series_positions(s))
    if s[0] == NESTED:
        assert len(slices) == min(s[2], s[4])  # along the shorter side


def test_x86_call_filter():
    from paw.transforms import x86_call_sites, x86_calls

    # the E8 at 0 (relative 0) and the E9 at 5 jumping back to itself (relative -5) both go
    # to position 5, and both become 5; an address whose top byte isn't 00 or FF is kept
    data = b"\xe8\x00\x00\x00\x00" + b"\xe9\xfb\xff\xff\xff" + b"\xe8\x12\x34\x56\x78"
    out = x86_calls(data, forward=True)
    assert out == b"\xe8\x05\x00\x00\x00" + b"\xe9\x05\x00\x00\x00" + data[10:]
    assert x86_calls(out, forward=False) == data
    # the address after an E8 is never scanned itself; nor is an E8 without 4 bytes after it
    assert list(x86_call_sites(b"\xe8\xe8\x00\x00\x00\x00")) == [0]
    assert list(x86_call_sites(b"\x90\xe8\x00\x00\x00")) == []
    # a sum that reaches bit 24 still has a top byte of 00 or FF: bit 24 is copied upwards
    assert x86_calls(b"\x90" * 3 + b"\xe8\xff\xff\xff\x00", forward=True)[4:] == b"\x07\x00\x00\xff"


def test_finds_run_and_progression():
    positions = sorted(set(range(1000, 1500)) | set(range(10, 3010, 30)) | {7})
    series, leftovers = select_series(positions)
    # the run must survive intact; the progression is split around it
    assert (ARITH, 1000, 1, 500) in series
    assert {s[2] for s in series} == {1, 30}
    assert leftovers == [7]


def test_finds_rectangle_of_rgb_pixels():
    # one colour channel of a 20x10 rectangle of 3-byte pixels in 300-byte rows
    positions = [5000 + row * 300 + col * 3 for row in range(10) for col in range(20)]
    series, leftovers = select_series(positions)
    assert series == [(NESTED, 5000, 20, 300, 10, 3)] and leftovers == []


def test_irregular_positions_get_no_series():
    positions = [0, 3, 11, 12, 30, 31, 47, 90, 151, 152]
    assert select_series(positions) == ([], positions)
