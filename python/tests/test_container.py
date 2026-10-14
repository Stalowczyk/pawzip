"""End-to-end behaviour of compress / decompress / info and the block framing."""

import io
import random
import zlib

import pytest

from paw import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_BLOCK_SIZE,
    FormatError,
    Mode,
    PawError,
    SizeLimitError,
    compress,
    decompress,
    decompress_stream,
    info,
)
from paw.blocks import BASIC_MODES, BWT_MODES, DELTA_MODES
from paw.varint import write_varint

from inputs import realistic_inputs


def varint(n):
    out = bytearray()
    write_varint(out, n)
    return bytes(out)


@pytest.mark.parametrize("mode", list(Mode))
def test_roundtrip_in_every_mode(case, mode):
    assert decompress(compress(case, modes=[mode])) == case


def test_roundtrip_default(case):
    assert decompress(compress(case)) == case


@pytest.mark.parametrize("block_size", [1, 2, 3, 100, 4096])
def test_roundtrip_with_small_blocks(case, block_size):
    data = case[:3000]
    blob = compress(data, block_size=block_size, modes=[Mode.STORED, Mode.MAP, Mode.BWT_MTF_MAP])
    assert decompress(blob) == data
    assert info(blob).block_count == -(-len(data) // block_size)


@pytest.mark.parametrize("seed", range(30))
def test_roundtrip_random_structured(seed):
    r = random.Random(seed)
    alphabet = r.randbytes(r.randint(1, 6))
    parts = []
    for _ in range(r.randint(1, 30)):
        chunk = bytes(r.choice(alphabet) for _ in range(r.randint(1, 50)))
        parts.append(chunk * r.randint(1, 20))
    data = b"".join(parts)
    assert decompress(compress(data, block_size=r.choice([64, 1000, DEFAULT_BLOCK_SIZE]))) == data


# --- inputs, modes and options ----------------------------------------------------------


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview], ids=lambda t: t.__name__)
def test_accepts_bytes_like_input(wrap):
    data = b"hello world\n" * 50
    blob = compress(wrap(data))
    assert isinstance(blob, bytes)
    assert decompress(wrap(blob)) == data
    assert info(wrap(blob)).original_size == len(data)


def test_modes_accept_enum_and_int():
    data = b"abcabcabc" * 100
    assert compress(data, modes=[Mode.MAP]) == compress(data, modes=[1])


@pytest.mark.parametrize("modes", [[], [8], ["map"]], ids=["empty", "unknown int", "string"])
def test_invalid_modes_are_rejected(modes):
    with pytest.raises(ValueError):
        compress(b"data", modes=modes)


@pytest.mark.parametrize("block_size", [0, -5, 2**32])
def test_invalid_block_size(block_size):
    with pytest.raises(ValueError):
        compress(b"data", block_size=block_size)
    with pytest.raises(TypeError):
        compress(b"data", block_size=1.5)


@pytest.mark.parametrize("workers", [0, -1, "many", 1.5, True])
def test_invalid_workers(workers):
    with pytest.raises(ValueError):
        compress(b"data", workers=workers)


def test_mode_labels():
    assert [m.label for m in Mode] == [
        "stored",
        "map",
        "bwt-mtf-map",
        "delta-map",
        "delta-bwt-mtf-map",
        "bwt-map",
        "delta-bwt-map",
        "x86-bwt-map",
    ]


def test_default_picks_smallest_of_basic_modes():
    data = b"hello world\n" * 1000
    best = min(len(compress(data, modes=[m])) for m in BASIC_MODES)
    assert len(compress(data)) <= best < 120


def test_each_block_chooses_its_own_mode():
    text = b"the quick brown fox jumps over the lazy dog. " * 40
    noise = random.Random(3).randbytes(len(text))
    meta = info(compress(text + noise, block_size=len(text)))
    assert meta.modes[0] != Mode.STORED and meta.modes[1] == Mode.STORED


def test_incompressible_blocks_are_stored_without_trying_every_mode(monkeypatch):
    import zlib as z

    from paw import blocks

    packed = z.compress(random.Random(1).randbytes(400_000) + b"text " * 60_000, 9)  # like a .zip
    tried = []
    monkeypatch.setattr(blocks, "encode_map", lambda d, **_: tried.append(len(d)) or bytes(len(d)))
    assert blocks.compress_block_pure(packed, None) == (Mode.STORED, packed)
    assert max(tried) <= blocks.PROBE_SLICE * blocks.PROBE_SLICES  # only the sample was tried
    # thorough=True tries every mode on the whole block anyway
    tried.clear()
    blocks.compress_block_pure(packed, None, thorough=True)
    assert max(tried) == len(packed)
    monkeypatch.undo()

    text = b"the cat and the hat sat on the mat. " * 10_000  # 360 KB of compressible data
    assert blocks._looks_incompressible(packed) and not blocks._looks_incompressible(text)


def test_random_data_is_stored_with_small_overhead():
    data = random.Random(5).randbytes(3000)
    blob = compress(data)
    assert info(blob).modes == (Mode.STORED,)
    assert len(blob) <= len(data) + 32  # header + one block header + end record


def test_empty_input():
    blob = compress(b"", modes=[Mode.BWT_MTF_MAP])
    meta = info(blob)
    assert decompress(blob) == b""
    assert meta.modes == () and meta.original_size == 0 and meta.ratio == 1.0
    assert meta.mode_summary == "-"


def x86_code(n_calls: int, seed: int = 3) -> bytes:
    """Bytes that look like x86 code: random instructions with many CALLs to a few places."""
    rng = random.Random(seed)
    targets = [rng.randrange(0, 60_000) for _ in range(12)]
    out = bytearray()
    for _ in range(n_calls):
        out += rng.randbytes(rng.randrange(2, 9)).replace(b"\xe8", b"\x90").replace(b"\xe9", b"\x90")
        out += b"\xe8" + ((rng.choice(targets) - len(out) - 5) % 2**32).to_bytes(4, "little")
    return bytes(out)


def test_x86_mode_is_tried_only_on_code_like_blocks():
    from paw import blocks

    code = x86_code(6000)
    assert blocks._looks_like_x86(code)
    assert not blocks._looks_like_x86(b"the map, the series and the gaps. " * 1000)
    assert not blocks._looks_like_x86(random.Random(9).randbytes(50_000))
    assert blocks.compress_block_pure(code, None)[0] == Mode.X86_BWT_MAP
    blob = compress(code)
    assert len(blob) < min(len(compress(code, modes=[m])) for m in BASIC_MODES)
    assert decompress(blob) == code


def test_delta_mode_chosen_for_smooth_data():
    ramp = bytes((i // 7) % 256 for i in range(20000))
    blob = compress(ramp)
    assert info(blob).modes[0] in DELTA_MODES
    assert decompress(blob) == ramp
    assert len(blob) < len(compress(ramp, modes=[Mode.MAP, Mode.BWT_MTF_MAP]))


# --- info and framing ---------------------------------------------------------------


def test_info_fields():
    data = b"hello world\n" * 1000
    blob = compress(data, block_size=5000)
    meta = info(blob)
    assert blob[:4] == b"PAW\x01"
    assert meta.original_size == len(data) and meta.compressed_size == len(blob)
    assert meta.block_size == 5000 and meta.block_count == 3
    assert meta.format_version == 1 and meta.ratio == len(blob) / len(data)
    with pytest.raises(AttributeError):
        meta.block_size = 1  # frozen


def test_exact_layout_of_a_stored_block():
    blob = compress(b"abc", modes=[Mode.STORED], block_size=1000)
    expected = b"PAW\x01" + varint(1000) + b"\x00\x00"  # no extension, no extra data
    crc = zlib.crc32(b"abc").to_bytes(4, "little")
    expected += varint(3) + bytes([Mode.STORED]) + crc + varint(3) + b"abc"
    expected += varint(0) + varint(3)
    assert blob == expected


# --- errors ---------------------------------------------------------------------------


def test_error_hierarchy():
    assert issubclass(FormatError, PawError) and issubclass(SizeLimitError, PawError)
    assert issubclass(PawError, ValueError)


GOOD = compress(b"the quick brown fox jumps over the lazy dog " * 20, modes=[Mode.MAP], block_size=300)


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda b: b"ZIP" + b[3:], id="bad magic"),
        pytest.param(lambda b: b[:3] + bytes([99]) + b[4:], id="unknown version"),
        pytest.param(lambda b: b[:4] + varint(0) + b[6:], id="zero block size"),
        pytest.param(lambda b: b[:4] + varint(10) + b[6:], id="block larger than block size"),
        pytest.param(lambda b: b[:10] + bytes([77]) + b[11:], id="unknown block mode"),
        pytest.param(lambda b: b[:11] + bytes([b[11] ^ 1]) + b[12:], id="wrong crc"),
        pytest.param(lambda b: b[:5], id="truncated header"),
        pytest.param(lambda b: b[: len(b) // 2], id="truncated body"),
        pytest.param(lambda b: b[:-1], id="missing total"),
        pytest.param(lambda b: b[:-1] + bytes([b[-1] ^ 1]), id="wrong total"),
        pytest.param(lambda b: b + b"x", id="trailing data"),
        pytest.param(lambda b: b"", id="empty"),
    ],
)
def test_rejects_invalid_files(mangle):
    assert GOOD[4:6] == varint(300)
    with pytest.raises(FormatError):
        decompress(mangle(GOOD))


def test_optional_header_data_is_skipped():
    data = b"the quick brown fox " * 50
    blob = compress(data)
    at = 4 + len(varint(DEFAULT_BLOCK_SIZE)) + 1  # magic, version, block size, extension length
    assert blob[at] == 0  # v1 encoders write no optional data
    later = blob[:at] + varint(5) + b"later" + blob[at + 1 :]  # what a later version may add
    assert decompress(later) == data
    too_long = blob[:at] + varint(2**16 + 1) + bytes(2**16 + 1) + blob[at + 1 :]
    with pytest.raises(FormatError, match="optional header data"):
        decompress(too_long)


def test_header_varint_over_64_bits():
    with pytest.raises(FormatError, match="64 bits"):
        decompress(b"PAW\x01" + b"\xff" * 9 + b"\x02")


def test_every_single_byte_corruption_is_handled():
    data = b"the quick brown fox jumps over the lazy dog " * 30
    blob = compress(data, modes=[Mode.MAP], block_size=500)
    for i in range(len(blob)):
        broken = bytearray(blob)
        broken[i] ^= 0x55
        try:
            result = decompress(bytes(broken))
        except PawError:
            continue
        assert result == data  # only harmless corruption (e.g. padding bits) may decode


def test_max_size():
    data = b"x" * 1000
    blob = compress(data, block_size=100)
    assert decompress(blob, max_size=1000) == data
    with pytest.raises(SizeLimitError):
        decompress(blob, max_size=999)


# A tiny "file" whose one block claims to hold 4 GiB.
BOMB = b"PAW\x01" + varint(2**32 - 1) + b"\x00\x00"  # no extension, no extra data
BOMB += varint(2**32 - 1) + bytes([Mode.MAP]) + bytes(4) + varint(5) + b"xxxxx"


def test_max_size_rejects_bomb_before_allocating():
    with pytest.raises(SizeLimitError, match="max_size"):
        decompress(BOMB, max_size=10_000_000, max_block_size=None)


def test_bomb_is_refused_by_default():
    assert DEFAULT_MAX_BLOCK_SIZE == 64 * 2**20
    with pytest.raises(SizeLimitError, match="max_block_size"):
        decompress(BOMB)


def test_max_block_size():
    data = b"x" * 1000
    blob = compress(data, block_size=400)
    assert decompress(blob, max_block_size=400) == data
    with pytest.raises(SizeLimitError, match="max_block_size"):
        decompress(blob, max_block_size=399)
    assert decompress(blob, max_block_size=None) == data
    # What counts is the data in a block, not the block size declared in the header.
    assert decompress(compress(data, block_size=2**31)) == data


@pytest.mark.slow
def test_parallel_workers_give_identical_output():
    data = realistic_inputs()["text"]
    serial = compress(data, block_size=20_000)
    assert compress(data, block_size=20_000, workers=2) == serial
    assert decompress(serial) == data


@pytest.mark.slow
@pytest.mark.parametrize("name", list(realistic_inputs()))
def test_realistic_inputs(name):
    data = realistic_inputs()[name]
    blob = compress(data)
    assert decompress(blob) == data
    assert len(blob) <= len(data) + 32  # header + one block header + end record
    if name != "random":
        assert len(blob) < len(zlib.compress(data, 9)) * 1.5


def test_bwt_walk_rows_are_stored_and_checked():
    from paw.transforms import bwt, walk_count, walk_rows
    from paw.varint import Reader

    rng = random.Random(4)
    words = b"the map series shape gap block paw".split()
    data = b" ".join(rng.choice(words) for _ in range(90_000))  # ~400 KB: 3 walks, 2 rows
    assert walk_count(len(data)) == 3
    blob = compress(data, modes=[Mode.BWT_MTF_MAP])
    assert decompress(blob) == data

    # find the block body: header (magic, version, block size, extension, extra), block header
    r = Reader(blob, 4)
    r.varint()
    r.take(r.byte())
    r.take(r.varint())
    assert r.varint() == len(data) and r.byte() == Mode.BWT_MTF_MAP
    r.take(4)  # CRC
    r.varint()  # body size
    last, primary = bwt(data)
    assert r.varint() == primary
    row_at = r.pos
    assert [r.varint(), r.varint()] == walk_rows(last, primary)

    damaged = bytearray(blob)
    damaged[row_at] ^= 0x01  # a stored row that no longer matches the walk
    with pytest.raises(FormatError, match="walk row"):
        decompress(bytes(damaged))


def test_modes_without_bwt_are_skipped_when_the_sample_says_they_cant_win(monkeypatch):
    from paw import blocks

    rng = random.Random(8)
    words = b"the map series shape gap block paw sample mode".split()
    text = b" ".join(rng.choice(words) for _ in range(60_000))  # ~300 KB of text
    full = []
    real = blocks._body
    monkeypatch.setattr(blocks, "_body", lambda d, m, *a: full.append((len(d), m)) or real(d, m, *a))
    assert blocks.compress_block_pure(text, None)[0] in BWT_MODES
    assert (len(text), Mode.MAP) not in full and (len(text), Mode.BWT_MAP) in full  # map skipped
    full.clear()
    blocks.compress_block_pure(text, None, thorough=True)
    assert (len(text), Mode.MAP) in full  # thorough tries it anyway
    monkeypatch.undo()

    # data where the plain map wins (records with arithmetic structure) keeps it
    records = b"".join(i.to_bytes(4, "little") + bytes(12) for i in range(20_000))
    assert blocks.compress_block(records, None) == blocks.compress_block(records, None, thorough=True)


def _multi_block():
    rng = random.Random(12)
    data = b"".join(rng.choice([b"map ", b"series ", b"gap ", b"paw "]) for _ in range(6000))
    return data, compress(data, block_size=2000)


@pytest.mark.parametrize("workers", [2, 3, "auto"])
def test_parallel_decompression_matches_serial(workers):
    data, blob = _multi_block()
    assert info(blob).block_count > 5
    assert decompress(blob, workers=workers) == data
    serial, parallel = [], []
    decompress_stream(io.BytesIO(blob), io.BytesIO(), progress=serial.append)
    decompress_stream(io.BytesIO(blob), io.BytesIO(), progress=parallel.append, workers=workers)
    assert parallel == serial  # progress reports exact positions, not read-ahead


def test_parallel_decompression_reports_errors_in_file_order():
    _, blob = _multi_block()
    rng = random.Random(4)
    for _ in range(40):
        damaged = bytearray(blob)
        for _ in range(2):  # two hits: the first one in file order must win
            damaged[rng.randrange(8, len(blob))] ^= 1 << rng.randrange(8)
        outcomes = []
        for workers in (1, 4):
            try:
                outcomes.append(decompress(bytes(damaged), workers=workers))
            except PawError as e:
                outcomes.append((type(e), str(e)))
        assert outcomes[0] == outcomes[1]
