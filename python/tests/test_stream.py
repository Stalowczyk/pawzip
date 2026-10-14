"""Streaming: compress_stream / decompress_stream, paw.open, and the parallel helpers."""

import io

import pytest

import paw
from paw.parallel import MEMORY_PER_BLOCK_BYTE, available_memory, ordered_map, resolve_workers

TEXT = "Zażółć gęślą jaźń. Line two.\n" * 200
DATA = TEXT.encode()


class Trickle(io.RawIOBase):
    """A pipe-like reader that returns at most 7 bytes per read."""

    def __init__(self, data):
        self.data, self.pos = data, 0

    def readable(self):
        return True

    def readinto(self, b):
        n = min(7, len(b), len(self.data) - self.pos)
        b[:n] = self.data[self.pos : self.pos + n]
        self.pos += n
        return n


# --- compress_stream / decompress_stream ----------------------------------------------


def test_stream_roundtrip_matches_compress():
    out = io.BytesIO()
    assert paw.compress_stream(io.BytesIO(DATA), out, block_size=1000) == len(DATA)
    assert out.getvalue() == paw.compress(DATA, block_size=1000)
    back = io.BytesIO()
    assert paw.decompress_stream(io.BytesIO(out.getvalue()), back) == len(DATA)
    assert back.getvalue() == DATA


def test_short_reads_still_make_full_blocks():
    out = io.BytesIO()
    paw.compress_stream(Trickle(DATA), out, block_size=1000)
    assert out.getvalue() == paw.compress(DATA, block_size=1000)


def test_progress_reports_input_bytes():
    seen = []
    out = io.BytesIO()
    paw.compress_stream(io.BytesIO(DATA), out, block_size=1000, progress=seen.append)
    blocks = -(-len(DATA) // 1000)
    assert seen == sorted(seen) and len(seen) == blocks + 1 and seen[-1] == len(DATA)
    assert seen[0] == 1000  # after the first block

    blob = out.getvalue()
    seen.clear()
    paw.decompress_stream(io.BytesIO(blob), io.BytesIO(), progress=seen.append)
    assert seen == sorted(seen) and len(seen) == blocks + 1 and seen[-1] == len(blob)


def test_progress_on_empty_input():
    seen = []
    out = io.BytesIO()
    paw.compress_stream(io.BytesIO(b""), out, progress=seen.append)
    assert seen == [0]
    seen.clear()
    paw.decompress_stream(io.BytesIO(out.getvalue()), io.BytesIO(), progress=seen.append)
    assert seen == [len(out.getvalue())]


def test_short_reads_when_decompressing():
    blob = paw.compress(DATA, block_size=1000)
    out = io.BytesIO()
    assert paw.decompress_stream(Trickle(blob), out) == len(DATA)
    assert out.getvalue() == DATA
    assert paw.info(Trickle(blob)).original_size == len(DATA)


def test_decompress_stream_max_size():
    blob = paw.compress(DATA, block_size=500)
    with pytest.raises(paw.SizeLimitError):
        paw.decompress_stream(io.BytesIO(blob), io.BytesIO(), max_size=1000)


def test_info_from_file_object_skips_bodies(tmp_path):
    path = tmp_path / "x.paw"
    path.write_bytes(paw.compress(DATA, block_size=1000))
    with open(path, "rb") as f:
        meta = paw.info(f)
    assert meta.original_size == len(DATA) and meta.block_count == -(-len(DATA) // 1000)
    assert meta.compressed_size == path.stat().st_size


def test_info_from_a_path(tmp_path):
    blob = paw.compress(DATA, block_size=1000)
    path = tmp_path / "x.paw"
    path.write_bytes(blob)
    assert paw.info(path) == paw.info(str(path)) == paw.info(blob)
    with pytest.raises(TypeError):
        paw.info(42)


def test_info_detects_truncated_body():
    blob = paw.compress(DATA, block_size=1000)
    with pytest.raises(paw.FormatError):
        paw.info(blob[:-20])


# --- paw.open -------------------------------------------------------------------------


def test_open_binary_write_then_read(tmp_path):
    path = tmp_path / "data.bin.paw"
    with paw.open(path, "wb", block_size=300) as f:
        for i in range(0, len(DATA), 123):  # many small writes
            f.write(DATA[i : i + 123])
    assert paw.decompress(path.read_bytes()) == DATA
    assert paw.info(path.read_bytes()).block_size == 300

    with paw.open(path) as f:
        assert f.read(10) == DATA[:10]
        assert f.read(500) == DATA[10:510]
        assert f.read() == DATA[510:]
        assert f.read() == b""


def test_open_text_mode(tmp_path):
    path = tmp_path / "notes.txt.paw"
    with paw.open(path, "wt", encoding="utf-8") as f:
        f.write(TEXT)
    with paw.open(path, "rt", encoding="utf-8") as f:
        lines = list(f)
    assert "".join(lines) == TEXT and len(lines) == 200


def test_open_readline_and_readinto(tmp_path):
    path = tmp_path / "x.paw"
    path.write_bytes(paw.compress(DATA, block_size=100))
    with paw.open(path, "rb") as f:
        assert f.readline() == DATA[: DATA.index(b"\n") + 1]
        buf = bytearray(5)
        assert f.readinto(buf) == 5
        assert f.peek(1)


def test_open_exclusive_mode(tmp_path):
    path = tmp_path / "x.paw"
    with paw.open(path, "xb") as f:
        f.write(b"once")
    with pytest.raises(FileExistsError):
        paw.open(path, "xb")


def test_open_wraps_existing_file_object():
    raw = io.BytesIO()
    with paw.open(raw, "wb") as f:
        f.write(DATA)
    assert not raw.closed  # the caller's object is not closed
    assert paw.decompress(raw.getvalue()) == DATA
    with paw.open(io.BytesIO(raw.getvalue())) as f:
        assert f.read() == DATA


def test_open_empty_file(tmp_path):
    path = tmp_path / "empty.paw"
    with paw.open(path, "wb"):
        pass
    assert paw.decompress(path.read_bytes()) == b""


def test_open_max_size(tmp_path):
    path = tmp_path / "x.paw"
    path.write_bytes(paw.compress(DATA, block_size=100))
    with paw.open(path, max_size=50) as f, pytest.raises(paw.SizeLimitError):
        f.read()
    with paw.open(path, max_block_size=99) as f, pytest.raises(paw.SizeLimitError):
        f.read()


def test_decompress_stream_max_block_size():
    blob = paw.compress(DATA, block_size=500)
    with pytest.raises(paw.SizeLimitError):
        paw.decompress_stream(io.BytesIO(blob), io.BytesIO(), max_block_size=499)


def test_open_wrong_direction_and_closed(tmp_path):
    path = tmp_path / "x.paw"
    with paw.open(path, "wb") as f:
        assert f.writable() and not f.readable()
        with pytest.raises(io.UnsupportedOperation):
            f.read()
    with pytest.raises(ValueError):
        f.write(b"late")
    with paw.open(path) as f, pytest.raises(io.UnsupportedOperation):
        f.write(b"x")


@pytest.mark.parametrize(
    ("mode", "kwargs"),
    [("ab", {}), ("rw", {}), ("rbt", {}), ("rr", {}), ("rb", {"encoding": "utf-8"})],
)
def test_open_rejects_bad_arguments(tmp_path, mode, kwargs):
    with pytest.raises(ValueError):
        paw.open(tmp_path / "x.paw", mode, **kwargs)


def test_open_rejects_non_files():
    with pytest.raises(TypeError):
        paw.open(42)


# --- parallel helpers -------------------------------------------------------------------


def test_available_memory_is_plausible():
    free = available_memory()
    assert free is None or free > 10 * 2**20


def test_resolve_workers_auto_respects_memory(monkeypatch):
    block = 16 * 2**20
    per_worker = MEMORY_PER_BLOCK_BYTE * block
    monkeypatch.setattr("paw.parallel.os.cpu_count", lambda: 8)
    monkeypatch.setattr("paw.parallel.available_memory", lambda: 3 * per_worker)  # room for 1.5
    assert resolve_workers("auto", block) == 1
    monkeypatch.setattr("paw.parallel.available_memory", lambda: 100 * per_worker)
    assert resolve_workers("auto", block) == 8  # capped by CPU count
    assert resolve_workers("auto", block, blocks=3) == 3  # never more than there are blocks
    monkeypatch.setattr("paw.parallel.available_memory", lambda: 0)
    assert resolve_workers("auto", block) == 1  # always at least one
    monkeypatch.setattr("paw.parallel.available_memory", lambda: None)
    assert resolve_workers("auto", block) == 1  # unknown memory: play safe


def test_resolve_workers_explicit():
    assert resolve_workers(4, 1000) == 4
    assert resolve_workers(4, 1000, blocks=2) == 2


def test_ordered_map_serial_and_parallel():
    assert list(ordered_map(abs, [-3, 1, -2], 1)) == [3, 1, 2]
    assert list(ordered_map(abs, iter(range(-20, 0)), 2)) == list(range(20, 0, -1))


@pytest.mark.slow
def test_compress_file_with_workers(tmp_path):
    src = tmp_path / "big.txt"
    src.write_bytes(DATA * 4)
    seen = []
    packed = paw.compress_file(src, block_size=2000, workers=2, progress=seen.append)
    assert packed.read_bytes() == paw.compress(DATA * 4, block_size=2000, extension="txt")
    # in block order and exact, although workers read ahead
    assert seen == sorted(seen) and seen[0] == 2000 and seen[-1] == len(DATA) * 4
