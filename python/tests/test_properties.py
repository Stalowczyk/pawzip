"""Property-based tests: invariants that must hold for any input Hypothesis can generate."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paw import FormatError, Mode, compress, decompress
from paw.arith import NUM_CLASSES, BitModel, Decoder, Encoder, Model
from paw.gaps import class_base, gap_class
from paw.series import select_series, series_positions
from paw.transforms import bwt, delta, inverse_bwt, inverse_delta, inverse_mtf, mtf
from paw.varint import Reader, write_varint

# Inputs with structure: a few chunks over a small alphabet, each repeated.
small_alphabet_chunk = st.integers(1, 4).flatmap(
    lambda k: st.binary(min_size=1, max_size=40).map(lambda b: bytes(x % k + 97 for x in b))
)
structured = st.lists(st.tuples(small_alphabet_chunk, st.integers(1, 30)), max_size=12).map(
    lambda parts: b"".join(chunk * times for chunk, times in parts)
)
any_bytes = st.binary(max_size=2000)


@given(st.one_of(any_bytes, structured))
def test_roundtrip(data):
    assert decompress(compress(data)) == data


@pytest.mark.parametrize("mode", list(Mode))
@given(data=st.one_of(st.binary(max_size=300), structured))
def test_roundtrip_forced_mode(mode, data):
    assert decompress(compress(data, modes=(mode,))) == data


@given(structured.filter(lambda d: len(d) > 20), st.data())
def test_corruption_never_crashes(data, draw):
    blob = bytearray(compress(data))
    action = draw.draw(st.sampled_from(["flip", "truncate", "append"]))
    if action == "flip":
        i = draw.draw(st.integers(0, len(blob) - 1))
        blob[i] ^= draw.draw(st.integers(1, 255))
    elif action == "truncate":
        del blob[draw.draw(st.integers(0, len(blob) - 1)) :]
    else:
        blob += draw.draw(st.binary(min_size=1, max_size=8))
    try:
        result = decompress(bytes(blob))
    except FormatError:
        return
    assert result == data


@given(st.integers(0, 2**64 - 1))
def test_varint_roundtrip(n):
    out = bytearray()
    write_varint(out, n)
    assert Reader(bytes(out)).varint() == n


@given(st.integers(0, 2**32 - 2))
def test_gap_class_roundtrip(gap):
    cls, nbits, extra = gap_class(gap)
    assert 0 <= cls < NUM_CLASSES
    base, bits = class_base(cls)
    assert bits == nbits and 0 <= extra < (1 << nbits or 1) and base + extra == gap


@given(st.lists(st.tuples(st.integers(0, NUM_CLASSES - 1), st.integers(0, 1)), max_size=500))
def test_arithmetic_coder_roundtrip(items):
    enc, model = Encoder(), Model()
    for sym, bit in items:
        enc.encode(model, sym)
        enc.encode_bit(bit)
    dec, model = Decoder(enc.finish()), Model()
    for sym, bit in items:
        assert dec.decode(model) == sym
        assert dec.decode_bit() == bit


@given(st.lists(st.tuples(st.integers(0, NUM_CLASSES - 1), st.integers(0, 1)), max_size=500))
def test_adaptive_bits_roundtrip(items):
    enc, model, bits = Encoder(), Model(), [BitModel(), BitModel()]
    for sym, bit in items:
        enc.encode(model, sym)
        enc.encode_adaptive_bit(bits[sym % 2], bit)
    dec, model, bits = Decoder(enc.finish()), Model(), [BitModel(), BitModel()]
    for sym, bit in items:
        assert dec.decode(model) == sym
        assert dec.decode_adaptive_bit(bits[sym % 2]) == bit


@given(st.one_of(any_bytes, structured))
def test_transforms_roundtrip(data):
    last, primary = bwt(data)
    assert sorted(last) == sorted(data)
    assert inverse_bwt(last, primary) == data
    assert inverse_mtf(mtf(data)) == data


@given(any_bytes, st.integers(1, 300))
def test_delta_roundtrip(data, distance):
    assert inverse_delta(delta(data, distance), distance) == data


@given(st.sets(st.integers(0, 5000), max_size=400))
def test_series_partition_positions(position_set):
    positions = sorted(position_set)
    series, leftovers = select_series(positions)
    covered = [p for s in series for p in series_positions(s)]
    assert len(covered) == len(set(covered))
    assert sorted(covered + leftovers) == positions
