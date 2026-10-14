"""The Rust core (paw._core) against the pure-Python reference: same bytes, same outcomes."""

import random

import pytest

from paw import FormatError, Mode, blocks

from inputs import realistic_inputs

pytestmark = pytest.mark.skipif(not blocks.NATIVE, reason="built without the Rust core")


def inputs():
    rng = random.Random(3)
    yield b"x"
    yield bytes(range(256)) * 3
    yield rng.randbytes(5000)
    yield b"the map, the series and the gaps. " * 300
    for data in realistic_inputs().values():
        yield data[:40_000]


MODE_CHOICES = [None, *[(m,) for m in Mode]]


@pytest.mark.parametrize("modes", MODE_CHOICES, ids=lambda m: "auto" if m is None else m[0].label)
def test_native_encoder_matches_the_reference(modes):
    for data in inputs():
        assert blocks.compress_block(data, modes) == blocks.compress_block_pure(data, modes)


def test_native_decoder_agrees_on_damaged_bodies():
    rng = random.Random(9)
    data = b"the map, the series and the gaps. " * 300
    for mode in Mode:
        if mode == Mode.STORED:
            continue
        body = blocks.compress_block_pure(data, (mode,))[1]
        for _ in range(60):
            damaged = bytearray(body)
            damaged[rng.randrange(len(body))] ^= 1 << rng.randrange(8)
            outcomes = []
            for decode in (blocks.decompress_block, blocks.decompress_block_pure):
                try:
                    outcomes.append(decode(mode, bytes(damaged), len(data)))
                except FormatError as e:
                    outcomes.append(("FormatError", str(e)))
            assert outcomes[0] == outcomes[1]
