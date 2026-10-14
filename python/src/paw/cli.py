"""Command line interface: ``paw compress | decompress | test | info``.

Everything streams block by block, so files of any size work with bounded memory.
Status messages go to stderr so that data written to stdout (``-c`` or ``-`` input) is
never mixed with them. Exit status: 0 on success, 1 if any file failed, 2 on usage errors,
130 if interrupted (Ctrl-C).
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import os
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from . import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_BLOCK_SIZE,
    Mode,
    PawError,
    __version__,
    compress_stream,
    decompress_stream,
    info,
)
from .files import SUFFIX, compressed_name, decompressed_name, write_atomically
from .parallel import Workers
from .stream import FORMAT_MAX_BLOCK_SIZE, Progress, Writable, read_extension

STDIO = "-"
Handler = Callable[[argparse.Namespace, str], None]
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}


def _size(text: str) -> int:
    """Parse a byte count such as ``5000``, ``64K``, ``100M`` or ``2G``."""
    text = text.strip().upper().removesuffix("B")
    unit = text[-1] if text and text[-1] in _UNITS else ""
    try:
        value = int(float(text[: len(text) - len(unit)]) * _UNITS[unit])
    except (ValueError, OverflowError):  # OverflowError: "inf", "1e400"
        raise argparse.ArgumentTypeError(f"invalid size: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("size must be at least 1 byte")
    return value


def _block_size(text: str) -> int:
    value = _size(text)
    if value > FORMAT_MAX_BLOCK_SIZE:
        raise argparse.ArgumentTypeError("block size must be below 4G")
    return value


def _workers(text: str) -> Workers:
    if text == "auto":
        return "auto"
    if text.isdigit() and int(text) >= 1:
        return int(text)
    raise argparse.ArgumentTypeError("workers must be a positive number or 'auto'")


def _mode(label: str) -> Mode:
    for mode in Mode:
        if label in (mode.label, str(mode.value)):
            return mode
    choices = ", ".join(m.label for m in Mode)
    raise argparse.ArgumentTypeError(f"unknown mode {label!r} (choose from {choices})")


class _Counter:
    """Wraps a binary writer and counts the bytes written through it."""

    def __init__(self, f: Writable) -> None:
        self.f = f
        self.count = 0

    def write(self, data: bytes) -> int:
        self.count += len(data)
        self.f.write(data)
        return len(data)


def _open_input(name: str) -> contextlib.AbstractContextManager[IO[bytes]]:
    if name == STDIO:
        return contextlib.nullcontext(sys.stdin.buffer)
    return builtins.open(name, "rb")


def _say(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message, file=sys.stderr)


# --- progress -------------------------------------------------------------------------


def _isatty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if isatty else False
    except ValueError:  # closed
        return False


def _show_progress(args: argparse.Namespace, to_stdout: bool) -> bool:
    """Progress goes to stderr, and only when someone is watching: never with --quiet, when
    stderr is redirected, or when the data itself is being written to the terminal."""
    if getattr(args, "quiet", False) or not _isatty(sys.stderr):
        return False
    return not (to_stdout and _isatty(sys.stdout))


def _input_size(f: IO[bytes]) -> int | None:
    """Size of the regular file being read, or None (pipes, terminals, in-memory objects)."""
    try:
        st = os.fstat(f.fileno())
    except (OSError, AttributeError, ValueError):
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def _amount(n: int) -> str:
    """A byte count for people: 900 B, 3.4 KiB, 120.0 MiB."""
    if n < 1024:
        return f"{n} B"
    value, unit = n / 1024, "KiB"
    for bigger in ("MiB", "GiB", "TiB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    return f"{value:.1f} {unit}"


def _clock(seconds: float) -> str:
    """0:07, 12:34 or 1:02:03."""
    s = int(seconds)
    hours, minutes, s = s // 3600, s // 60 % 60, s % 60
    return f"{hours}:{minutes:02d}:{s:02d}" if hours else f"{minutes}:{s:02d}"


def _progress_text(name: str, done: int, total: int | None, elapsed: float) -> str:
    if not total:  # size unknown (a pipe), or an empty file
        return f"{name}: {_amount(done)} so far, {_clock(elapsed)} elapsed"
    text = f"{name}: {100 * min(done, total) // total:3d}% of {_amount(total)}, {_clock(elapsed)} elapsed"
    if 0 < done < total:
        text += f", about {_clock(elapsed * (total - done) / done)} left"
    return text


def _columns() -> int:
    try:
        return os.get_terminal_size(sys.stderr.fileno()).columns
    except (OSError, AttributeError, ValueError):
        return 80


class _ProgressLine:
    """One line of progress on stderr, redrawn after every block and once a second in
    between (a single block can take minutes in pure Python), and erased at the end."""

    def __init__(self, name: str, total: int | None) -> None:
        self.name = "stdin" if name == STDIO else name
        self.total = total
        self.done = 0
        self.start = time.perf_counter()
        self.shown = 0  # length of the text currently on screen
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.ticker = threading.Thread(target=self._tick, daemon=True)

    def __enter__(self) -> _ProgressLine:
        self.draw()
        self.ticker.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stopped.set()
        self.ticker.join()
        with self.lock:
            sys.stderr.write("\r" + " " * self.shown + "\r")
            sys.stderr.flush()

    def update(self, done: int) -> None:
        self.done = done
        self.draw()

    def _tick(self) -> None:
        while not self.stopped.wait(1):
            self.draw()

    def draw(self) -> None:
        with self.lock:
            width = _columns() - 1
            elapsed = time.perf_counter() - self.start
            line = _progress_text(self.name, self.done, self.total, elapsed)
            if len(line) > width:  # shorten the name, keeping its end
                keep = max(len(self.name) - (len(line) - width) - 3, 8)
                line = _progress_text("..." + self.name[-keep:], self.done, self.total, elapsed)
            line = line[:width]
            sys.stderr.write("\r" + line.ljust(self.shown))
            sys.stderr.flush()
            self.shown = len(line)


def _progress(
    args: argparse.Namespace, name: str, src: IO[bytes], to_stdout: bool
) -> contextlib.AbstractContextManager[_ProgressLine | None]:
    if not _show_progress(args, to_stdout):
        return contextlib.nullcontext()
    return _ProgressLine(name, _input_size(src))


# --- commands -------------------------------------------------------------------------


def _run(
    args: argparse.Namespace,
    name: str,
    default_dst: Path | None,
    convert: Callable[[IO[bytes], Writable, Progress | None], int],
) -> tuple[str, int, int]:
    """Stream ``name`` through ``convert`` to stdout or a file, showing progress.

    Returns (destination description, bytes read by convert's count, bytes written).
    """
    to_stdout = args.stdout or (name == STDIO and args.output is None)
    with _open_input(name) as src, _progress(args, name, src, to_stdout) as line:
        progress = line.update if line else None
        if to_stdout:
            out = _Counter(sys.stdout.buffer)
            processed = convert(src, out, progress)
            sys.stdout.buffer.flush()
            return "stdout", processed, out.count
        dst = Path(args.output) if args.output else default_dst
        if dst is None:
            raise ValueError(f"has no {SUFFIX} suffix; use -o to name the output")
        if dst.exists() and not args.force:
            raise FileExistsError(f"{dst} already exists (use -f to overwrite)")
        result: dict[str, int] = {}

        def produce(f: IO[bytes]) -> None:
            counter = _Counter(f)
            result["processed"] = convert(src, counter, progress)
            result["written"] = counter.count

        write_atomically(dst, produce, args.force)
        return str(dst), result["processed"], result["written"]


def _compress(args: argparse.Namespace, name: str) -> None:
    start = time.perf_counter()
    default, extension = (None, None) if name == STDIO else compressed_name(Path(name))
    where, original, written = _run(
        args,
        name,
        default,
        lambda src, dst, progress: compress_stream(
            src,
            dst,
            modes=args.mode,
            block_size=args.block_size,
            workers=args.workers,
            progress=progress,
            extension=extension,
            thorough=args.thorough,
        ),
    )
    ratio = f"{100 * written / original:.1f}%" if original else "-"
    _say(
        args,
        f"{name} -> {where}: {original:,} -> {written:,} bytes ({ratio}), {time.perf_counter() - start:.1f}s",
    )


def _decompress(args: argparse.Namespace, name: str) -> None:
    default = None
    if name != STDIO and not args.output and not args.stdout:
        with builtins.open(name, "rb") as f:  # the output name depends on the stored extension
            default = decompressed_name(Path(name), read_extension(f))
    where, original, _ = _run(
        args,
        name,
        default,
        lambda src, dst, progress: decompress_stream(
            src,
            dst,
            max_size=args.max_size,
            max_block_size=args.max_block_size,
            progress=progress,
            workers=args.workers,
        ),
    )
    _say(args, f"{name} -> {where}: {original:,} bytes")


class _Discard:
    def write(self, data: bytes) -> int:
        return len(data)


def _test(args: argparse.Namespace, name: str) -> None:
    with _open_input(name) as src, _progress(args, name, src, to_stdout=False) as line:
        size = decompress_stream(
            src,
            _Discard(),
            max_size=args.max_size,
            max_block_size=args.max_block_size,
            progress=line.update if line else None,
            workers=args.workers,
        )
    print(f"{name}: OK ({size:,} bytes)")


def _info(args: argparse.Namespace, name: str) -> None:
    with _open_input(name) as src:
        meta = info(src)
    print(
        f"{name}\n"
        f"  original:    {meta.original_size:,} bytes\n"
        f"  compressed:  {meta.compressed_size:,} bytes ({100 * meta.ratio:.1f}%)\n"
        f"  blocks:      {meta.block_count:,} (block size {meta.block_size:,} bytes)\n"
        f"  modes:       {meta.mode_summary}\n"
        f"  extension:   {'.' + meta.extension if meta.extension else '-'}\n"
        f"  format:      v{meta.format_version}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paw", description="Compress and decompress .paw files.")
    parser.add_argument("--version", action="version", version=f"pawzip {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, func: Handler, help_text: str, writes: bool) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("files", nargs="+", metavar="FILE", help=f"input files ({STDIO} for stdin)")
        if writes:
            p.add_argument("-o", "--output", help="output file (only with a single input)")
            p.add_argument("-c", "--stdout", action="store_true", help="write to stdout")
            p.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
            p.add_argument("-q", "--quiet", action="store_true", help="no status messages")
        if name != "info":
            p.add_argument(
                "-T",
                "--workers",
                type=_workers,
                default=1,
                help="blocks coded in parallel, or 'auto' to fit the CPU and free memory (default 1)",
            )
        p.set_defaults(func=func)
        return p

    c = command("compress", _compress, "Compress FILE to FILE.paw.", writes=True)
    c.add_argument(
        "-m",
        "--mode",
        type=_mode,
        action="append",
        help="try only this mode (repeatable): " + ", ".join(m.label for m in Mode),
    )
    c.add_argument(
        "-B",
        "--block-size",
        type=_block_size,
        default=DEFAULT_BLOCK_SIZE,
        help="block size, e.g. 4M (default 16M); compressing needs about 100x this per worker, "
        "and blocks over 64M need --max-block-size to decompress",
    )
    c.add_argument(
        "--thorough",
        action="store_true",
        help="try every mode even on already-compressed data (JPEG, ZIP...): much slower, saves a little",
    )
    for name, func, help_text, writes in (
        ("decompress", _decompress, "Decompress FILE.paw to FILE.", True),
        ("test", _test, "Check that .paw files decompress correctly.", False),
    ):
        p = command(name, func, help_text, writes)
        p.add_argument("--max-size", type=_size, help="refuse outputs larger than this (e.g. 500M)")
        p.add_argument(
            "--max-block-size",
            type=_size,
            default=DEFAULT_MAX_BLOCK_SIZE,
            help="refuse blocks larger than this (default 64M; decoding needs about 20x a block)",
        )
    command("info", _info, "Show the headers of .paw files.", writes=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "output", None) and len(args.files) > 1:
        parser.error("-o/--output needs exactly one input file")
    if args.command == "compress" and not args.output:
        to_stdout = len(args.files) if args.stdout else args.files.count(STDIO)
        if to_stdout > 1:  # several streams back to back would not be a valid .paw file
            parser.error("compress can write only one input to stdout")

    status = 0
    try:
        for name in args.files:
            try:
                args.func(args, name)
            except (PawError, OSError, ValueError, MemoryError) as e:
                print(f"paw: {name}: {_describe(e, args.command)}", file=sys.stderr)
                if args.command == "test":
                    print(f"{name}: FAILED")
                status = 1
    except KeyboardInterrupt:  # partial outputs are already removed by write_atomically
        print("paw: interrupted", file=sys.stderr)
        return 130
    return status


def _describe(error: Exception, command: str) -> str:
    if isinstance(error, MemoryError):
        hint = " (a smaller --block-size needs less)" if command == "compress" else ""
        return "not enough memory" + hint
    return str(error)


if __name__ == "__main__":
    sys.exit(main())
