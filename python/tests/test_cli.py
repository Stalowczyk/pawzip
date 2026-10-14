"""The `paw` command line."""

import io
import re
import subprocess
import sys

import pytest

import paw.cli
from paw import Mode, __version__, decompress, info
from paw.cli import _amount, _clock, _progress_text, main

from inputs import huge_body_header

DATA = b"hello world\n" * 300


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "notes.txt"
    path.write_bytes(DATA)
    return path


class FakeStdio:
    """Stand-in for sys.stdin / sys.stdout with a binary .buffer."""

    def __init__(self, data=b""):
        self.buffer = io.BytesIO(data)


def test_compress_decompress_roundtrip(sample, tmp_path, capsys):
    assert main(["compress", "notes.txt"]) == 0
    assert (tmp_path / "notes.paw").stat().st_size < 200
    assert "notes.txt -> notes.paw" in capsys.readouterr().err  # status goes to stderr

    assert main(["decompress", "notes.paw", "-o", "restored.txt"]) == 0
    assert (tmp_path / "restored.txt").read_bytes() == DATA


def test_several_files_and_quiet(sample, tmp_path, capsys):
    (tmp_path / "b.txt").write_bytes(b"other" * 100)
    assert main(["compress", "-q", "notes.txt", "b.txt"]) == 0
    assert capsys.readouterr().err == ""
    assert (tmp_path / "b.paw").exists() and (tmp_path / "notes.paw").exists()


def test_output_needs_single_file(sample):
    with pytest.raises(SystemExit) as exit_info:
        main(["compress", "notes.txt", "notes.txt", "-o", "x.paw"])
    assert exit_info.value.code == 2


@pytest.mark.parametrize("argv", [["-c", "notes.txt", "b.txt"], ["-", "-"]], ids=["-c", "stdin twice"])
def test_compress_to_stdout_needs_single_input(sample, tmp_path, argv):
    (tmp_path / "b.txt").write_bytes(b"other")
    with pytest.raises(SystemExit) as exit_info:
        main(["compress", *argv])
    assert exit_info.value.code == 2


def test_thorough_option_reaches_the_encoder(sample, monkeypatch):
    seen = []
    real = paw.cli.compress_stream

    def spy(*args, **kwargs):
        seen.append(kwargs["thorough"])
        return real(*args, **kwargs)

    monkeypatch.setattr("paw.cli.compress_stream", spy)
    assert main(["compress", "notes.txt"]) == 0
    assert main(["compress", "-f", "--thorough", "notes.txt"]) == 0
    assert seen == [False, True]


def test_overwrite_protection(sample, capsys):
    main(["compress", "notes.txt"])
    assert main(["compress", "notes.txt"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["compress", "-f", "notes.txt"]) == 0


def test_mode_option(sample, tmp_path):
    assert main(["compress", "-m", "stored", "notes.txt"]) == 0
    assert info((tmp_path / "notes.paw").read_bytes()).modes == (Mode.STORED,)
    with pytest.raises(SystemExit):
        main(["compress", "-m", "nonsense", "notes.txt"])


def test_block_size_and_workers_options(sample, tmp_path):
    assert main(["compress", "-B", "1K", "-T", "1", "notes.txt"]) == 0
    meta = info((tmp_path / "notes.paw").read_bytes())
    assert meta.block_size == 1024 and meta.block_count == 4
    assert main(["test", "-T", "3", "notes.paw"]) == 0
    original = (tmp_path / "notes.txt").read_bytes()
    assert main(["decompress", "-T", "auto", "-o", "copy.txt", "notes.paw"]) == 0
    assert (tmp_path / "copy.txt").read_bytes() == original
    for bad in (["-B", "0"], ["-B", "5G"], ["-B", "lots"], ["-B", "inf"], ["-T", "0"], ["-T", "x"]):
        with pytest.raises(SystemExit) as exit_info:
            main(["compress", *bad, "notes.txt"])
        assert exit_info.value.code == 2
    with pytest.raises(SystemExit) as exit_info:
        main(["test", "--max-size", "1e400", "notes.paw"])
    assert exit_info.value.code == 2


def test_out_of_memory_is_reported(sample, tmp_path, monkeypatch, capsys):
    def no_memory(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr("paw.cli.compress_stream", no_memory)
    assert main(["compress", "notes.txt"]) == 1
    assert "notes.txt: not enough memory (a smaller --block-size" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]  # no partial output


def test_interrupt_exits_130_without_partial_output(sample, tmp_path, monkeypatch, capsys):
    def interrupted(src, dst, **kwargs):
        dst.write(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr("paw.cli.compress_stream", interrupted)
    assert main(["compress", "notes.txt"]) == 130
    assert "interrupted" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


def test_pipes(sample, monkeypatch):
    out = FakeStdio()
    monkeypatch.setattr(sys, "stdin", FakeStdio(DATA))
    monkeypatch.setattr(sys, "stdout", out)
    assert main(["compress", "-"]) == 0
    blob = out.buffer.getvalue()
    assert decompress(blob) == DATA

    out = FakeStdio()
    monkeypatch.setattr(sys, "stdin", FakeStdio(blob))
    monkeypatch.setattr(sys, "stdout", out)
    assert main(["decompress", "-"]) == 0
    assert out.buffer.getvalue() == DATA


def test_stdout_flag(sample, monkeypatch):
    main(["compress", "notes.txt"])
    out = FakeStdio()
    monkeypatch.setattr(sys, "stdout", out)
    assert main(["decompress", "-c", "notes.paw"]) == 0
    assert out.buffer.getvalue() == DATA


def test_test_command(sample, tmp_path, capsys):
    main(["compress", "notes.txt"])
    (tmp_path / "bad.paw").write_bytes(b"PAW\x01\x05\x05\x01junk")
    assert main(["test", "notes.paw"]) == 0
    assert "notes.paw: OK" in capsys.readouterr().out
    assert main(["test", "notes.paw", "bad.paw"]) == 1
    captured = capsys.readouterr()
    assert "bad.paw: FAILED" in captured.out and "paw: bad.paw:" in captured.err


def test_huge_declared_body_fails_cleanly(sample, tmp_path, capsys):
    (tmp_path / "evil.paw").write_bytes(huge_body_header())
    assert main(["test", "evil.paw"]) == 1
    assert "unexpected end of data" in capsys.readouterr().err


def test_max_size_option(sample, capsys):
    main(["compress", "notes.txt"])
    assert main(["test", "--max-size", "1K", "notes.paw"]) == 1
    assert "max_size" in capsys.readouterr().err
    assert main(["test", "--max-size", "1M", "notes.paw"]) == 0


def test_max_block_size_option(sample, capsys):
    main(["compress", "-B", "2K", "notes.txt"])
    assert main(["test", "--max-block-size", "1K", "notes.paw"]) == 1
    assert "max_block_size" in capsys.readouterr().err
    assert main(["test", "notes.paw"]) == 0  # the default (64M) allows it
    assert main(["decompress", "--max-block-size", "2K", "notes.paw", "-o", "back.txt"]) == 0


def test_info(sample, capsys):
    main(["compress", "notes.txt"])
    assert main(["info", "notes.paw"]) == 0
    out = capsys.readouterr().out
    assert "original:    3,600 bytes" in out and "modes:" in out and "format:      v1" in out
    assert "blocks:      1 (block size 16,777,216 bytes)" in out


def test_decompress_without_paw_suffix_needs_output(sample, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", FakeStdio(DATA))
    main(["compress", "-", "-o", "data.bin"])  # from stdin: no extension to store
    assert main(["decompress", "data.bin"]) == 1
    assert "-o" in capsys.readouterr().err


def test_stored_extension_survives_renaming(sample, tmp_path, capsys):
    assert main(["compress", "notes.txt"]) == 0  # -> notes.paw, remembering "txt"
    (tmp_path / "notes.paw").rename(tmp_path / "abc.paw")
    (tmp_path / "notes.txt").unlink()
    assert main(["decompress", "abc.paw"]) == 0
    assert (tmp_path / "abc.txt").read_bytes() == DATA
    assert main(["info", "abc.paw"]) == 0
    assert "extension:   .txt" in capsys.readouterr().out


def test_damaged_file_reports_error(sample, tmp_path, capsys):
    main(["compress", "notes.txt"])
    packed = tmp_path / "notes.paw"
    packed.write_bytes(b"ZIP" + packed.read_bytes()[3:])
    assert main(["decompress", "notes.paw", "-o", "out"]) == 1
    assert "not a .paw file" in capsys.readouterr().err


def test_missing_file(sample, capsys):
    assert main(["compress", "nope.txt"]) == 1
    assert "nope.txt" in capsys.readouterr().err


def test_progress_text():
    assert _amount(900) == "900 B" and _amount(3500) == "3.4 KiB" and _amount(130 * 2**20) == "130.0 MiB"
    assert _clock(7) == "0:07" and _clock(754) == "12:34" and _clock(3723) == "1:02:03"
    assert _progress_text("big.iso", 0, 2**20, 5) == "big.iso:   0% of 1.0 MiB, 0:05 elapsed"
    assert _progress_text("big.iso", 2**18, 2**20, 60) == (
        "big.iso:  25% of 1.0 MiB, 1:00 elapsed, about 3:00 left"
    )
    assert _progress_text("stdin", 3 * 2**20, None, 65) == "stdin: 3.0 MiB so far, 1:05 elapsed"


def test_no_progress_unless_stderr_is_a_terminal(sample, capsys):
    main(["compress", "-B", "1K", "notes.txt"])
    assert "\r" not in capsys.readouterr().err  # under pytest stderr is not a terminal


@pytest.mark.parametrize(
    "argv",
    [
        ["compress", "-B", "1K", "-f", "notes.txt"],
        ["decompress", "-f", "notes.paw"],
        ["test", "notes.paw"],
    ],
    ids=["compress", "decompress", "test"],
)
def test_progress_line(sample, monkeypatch, capsys, argv):
    main(["compress", "-q", "-B", "1K", "notes.txt"])
    monkeypatch.setattr("paw.cli._show_progress", lambda args, to_stdout: True)
    assert main(argv) == 0
    err = capsys.readouterr().err
    assert "\rnotes." in err and "100% of" in err  # drawn, ending at 100%
    assert re.search(r"100% of [^\r]*\r +\r", err)  # ...then erased in place


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_module_entry_point(sample):
    result = subprocess.run([sys.executable, "-m", "paw", "test", "missing.paw"], capture_output=True)
    assert result.returncode == 1
