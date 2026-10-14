"""compress_file / decompress_file."""

import os
import stat
from pathlib import Path

import pytest

import paw
from paw import FormatError, Mode, SizeLimitError, compress, compress_file, decompress_file, info

from inputs import huge_body_header

DATA = b"hello world\n" * 300


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(DATA)
    return path


def test_default_names_roundtrip(sample):
    packed = compress_file(sample)
    assert packed == sample.with_name("notes.paw") and packed.stat().st_size < 200
    sample.unlink()
    restored = decompress_file(packed)
    assert restored == sample and restored.read_bytes() == DATA


def test_explicit_destinations_and_str_paths(sample, tmp_path):
    packed = compress_file(str(sample), str(tmp_path / "out.bin"))
    assert packed == tmp_path / "out.bin"
    restored = decompress_file(packed, tmp_path / "back.txt")
    assert restored.read_bytes() == DATA


def test_progress_is_passed_through(sample, tmp_path):
    seen = []
    packed = compress_file(sample, block_size=1000, progress=seen.append)
    assert seen[-1] == len(DATA)
    seen.clear()
    decompress_file(packed, tmp_path / "back.txt", progress=seen.append)
    assert seen[-1] == packed.stat().st_size


def test_modes_are_passed_through(sample):
    assert info(compress_file(sample, modes=[Mode.STORED]).read_bytes()).modes == (Mode.STORED,)


def test_streams_in_blocks(sample, tmp_path):
    packed = compress_file(sample, block_size=1000)
    assert info(packed.read_bytes()).block_count == 4
    restored = decompress_file(packed, tmp_path / "back.txt")
    assert restored.read_bytes() == DATA


def test_refuses_to_overwrite(sample):
    packed = compress_file(sample)
    with pytest.raises(FileExistsError):
        compress_file(sample)
    compress_file(sample, overwrite=True)
    with pytest.raises(FileExistsError):
        decompress_file(packed)  # notes.txt still exists
    assert decompress_file(packed, overwrite=True).read_bytes() == DATA


def test_decompress_needs_paw_suffix_or_destination(tmp_path):
    packed = tmp_path / "data.bin"
    packed.write_bytes(compress(DATA))  # no extension stored
    with pytest.raises(ValueError, match=r"\.paw"):
        decompress_file(packed)
    assert decompress_file(packed, tmp_path / "x").read_bytes() == DATA


@pytest.mark.parametrize(
    ("source", "packed", "renamed", "restored"),
    [
        ("photo.png", "photo.paw", "abc.paw", "abc.png"),
        ("README", "README.paw", "README.paw", "README"),
        ("backup.tar.gz", "backup.tar.paw", "backup.tar.paw", "backup.tar.gz"),
        ("old.paw", "old.paw.paw", "old.paw.paw", "old.paw"),  # never overwrites itself
        ("x.we ird", "x.we ird.paw", "x.we ird.paw", "x.we ird"),  # unstorable: whole name kept
    ],
)
def test_names_and_stored_extensions(tmp_path, source, packed, renamed, restored):
    (tmp_path / source).write_bytes(DATA)
    out = compress_file(tmp_path / source)
    assert out.name == packed
    out = out.rename(tmp_path / renamed)
    (tmp_path / source).unlink()
    assert decompress_file(out).name == restored
    assert (tmp_path / restored).read_bytes() == DATA


def test_gzip_style_names_work(tmp_path):
    # photo.png.paw holding "png" (or nothing) decompresses to photo.png, not photo.png.png
    for stored in ("png", None):
        packed = tmp_path / "photo.png.paw"
        packed.write_bytes(compress(DATA, extension=stored))
        assert decompress_file(packed, overwrite=True).name == "photo.png"


def test_extension_validation():
    assert info(compress(b"x", extension=".PNG")).extension == "PNG"
    for bad in ("../png", "a" * 17, "p/n", "pn g", "ü"):
        with pytest.raises(ValueError):
            compress(b"x", extension=bad)


def test_failure_leaves_no_partial_output(sample, tmp_path):
    bad = tmp_path / "bad.txt.paw"
    bad.write_bytes(b"PAW garbage")
    with pytest.raises(FormatError):
        decompress_file(bad)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.txt.paw", "notes.txt"]


def test_binary_output_is_written_unchanged(tmp_path):
    data = bytes(range(256)) * 20  # includes \n and \r\n, which a text-mode file would alter
    src = tmp_path / "all.bin"
    src.write_bytes(data)
    restored = decompress_file(compress_file(src), tmp_path / "back.bin")
    assert restored.read_bytes() == data


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_outputs_get_the_usual_permissions(sample, tmp_path):
    old = os.umask(0o027)
    try:
        packed = compress_file(sample)
        restored = decompress_file(packed, tmp_path / "back.txt")
    finally:
        os.umask(old)
    for path in (packed, restored):
        assert stat.S_IMODE(path.stat().st_mode) == 0o640  # 0o666 minus the umask, not 0o600


def test_huge_declared_body_is_a_format_error(tmp_path):
    # Read from a real file: buffered file reads pre-allocate the requested size.
    evil = tmp_path / "evil.paw"
    evil.write_bytes(huge_body_header())
    with pytest.raises(FormatError):
        decompress_file(evil, tmp_path / "out")
    with paw.open(evil) as f, pytest.raises(FormatError):
        f.read()
    with open(evil, "rb") as f, pytest.raises(FormatError):
        info(f)
    assert not (tmp_path / "out").exists()


def test_max_size_is_passed_through(sample, tmp_path):
    packed = compress_file(sample)
    with pytest.raises(SizeLimitError):
        decompress_file(packed, tmp_path / "x", max_size=10)
    with pytest.raises(SizeLimitError):
        decompress_file(packed, tmp_path / "x", max_block_size=10)
    assert not Path(tmp_path / "x").exists()


def test_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        compress_file(tmp_path / "nope.txt")
