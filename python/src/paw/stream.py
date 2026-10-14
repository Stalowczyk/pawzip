"""Block framing of .paw files, streaming compression and the ``paw.open`` file object.

Layout (see spec/FORMAT.md)::

    "PAW" | version | block size
    block:  original size (>= 1) | mode | CRC-32 | body size | body
    ...
    end:    0 | total original size

Everything here works block by block, so memory use depends on the block size, not on
the size of the file.
"""

from __future__ import annotations

import builtins
import io
import os
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import IO, Literal, Protocol, overload

from .blocks import Mode, compress_block, decompress_block, resolve_modes
from .errors import FormatError, SizeLimitError
from .parallel import Workers, decode_memory_per_byte, ordered_map, resolve_workers
from .varint import read_varint, write_varint


class Writable(Protocol):
    """Anything with a binary ``write`` method (files, BytesIO, sys.stdout.buffer, ...)."""

    def write(self, data: bytes, /) -> object: ...


Progress = Callable[[int], object]
"""Called with the number of input bytes processed so far (after each block and at the end)."""

MAGIC = b"PAW"
FORMAT_VERSION = 1
DEFAULT_BLOCK_SIZE = 16 * 1024 * 1024
# Decoding a block needs about 20x its size in memory, so by default decoders refuse blocks
# above this size: a tiny damaged or malicious file could otherwise claim a 4 GiB block.
DEFAULT_MAX_BLOCK_SIZE = 64 * 1024 * 1024
FORMAT_MAX_BLOCK_SIZE = (1 << 32) - 1  # positions inside a block must fit the 4 GiB gap classes
READ_CHUNK = 1 << 20  # largest single read, so a corrupt length can't force a huge allocation


def check_block_size(block_size: int) -> int:
    if isinstance(block_size, bool) or not isinstance(block_size, int):
        raise TypeError("block_size must be an integer")
    if not 1 <= block_size <= FORMAT_MAX_BLOCK_SIZE:
        raise ValueError(f"block_size must be between 1 and {FORMAT_MAX_BLOCK_SIZE:,} bytes")
    return block_size


# --- writing ------------------------------------------------------------------------


MAX_EXTENSION = 16
# Optional header data that decoders skip (written empty by v1 encoders; see spec).
MAX_HEADER_EXTRA = 1 << 16
_EXTENSION_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def check_extension(extension: str | None) -> str | None:
    """Validate the original file's extension to store in the header ("png" or ".png").

    Only letters, digits, ``-`` and ``_``, at most 16 of them: it becomes part of a file
    name when decompressing, so it must never be able to point anywhere else.
    """
    if extension is None:
        return None
    name = extension.removeprefix(".")
    if not name:
        return None
    raw = name.encode("ascii", "replace")
    if len(raw) > MAX_EXTENSION or not set(raw) <= _EXTENSION_CHARS:
        raise ValueError(f"extension must be up to {MAX_EXTENSION} letters, digits, - or _: {extension!r}")
    return name


def _header(block_size: int, extension: str | None = None) -> bytes:
    out = bytearray(MAGIC)
    out.append(FORMAT_VERSION)
    write_varint(out, block_size)
    name = (extension or "").encode("ascii")
    out.append(len(name))
    out += name
    write_varint(out, 0)  # no optional header data
    return bytes(out)


def _end(total: int) -> bytes:
    out = bytearray()
    write_varint(out, 0)
    write_varint(out, total)
    return bytes(out)


Job = tuple[bytes, tuple[Mode, ...] | None, bool]  # block, modes to try, thorough


def encode_block(job: Job) -> bytes:
    """Compress one block and frame it. Top-level so worker processes can run it."""
    data, modes, thorough = job
    mode, body = compress_block(data, modes, thorough)
    out = bytearray()
    write_varint(out, len(data))
    out.append(mode)
    out += zlib.crc32(data).to_bytes(4, "little")
    write_varint(out, len(body))
    return bytes(out) + body


def _chunks(src: IO[bytes], size: int) -> Iterator[bytes]:
    """Read ``src`` in pieces of exactly ``size`` bytes (the last may be shorter)."""
    while True:
        chunk = src.read(size)
        if not chunk:
            return
        while len(chunk) < size:  # pipes may return less than asked for
            more = src.read(size - len(chunk))
            if not more:
                break
            chunk += more
        yield chunk
        if len(chunk) < size:
            return


def write_blocks(
    chunks: Iterable[bytes],
    dst: Writable,
    *,
    modes: tuple[Mode, ...] | None,
    block_size: int,
    workers: int,
    progress: Progress | None = None,
    extension: str | None = None,
    thorough: bool = False,
) -> int:
    """Write a complete .paw stream for already-cut blocks; return the original size."""
    dst.write(_header(block_size, extension))
    total = 0
    sizes: deque[int] = deque()  # blocks in flight, in order (workers read ahead)

    def jobs() -> Iterator[Job]:
        nonlocal total
        for chunk in chunks:
            total += len(chunk)
            sizes.append(len(chunk))
            yield chunk, modes, thorough

    done = 0
    for framed in ordered_map(encode_block, jobs(), workers):
        dst.write(framed)
        done += sizes.popleft()
        if progress is not None:
            progress(done)
    dst.write(_end(total))
    if progress is not None:
        progress(total)
    return total


def compress_stream(
    src: IO[bytes],
    dst: Writable,
    *,
    modes: Iterable[Mode | int] | None = None,
    block_size: int = DEFAULT_BLOCK_SIZE,
    workers: Workers = 1,
    progress: Progress | None = None,
    extension: str | None = None,
    thorough: bool = False,
) -> int:
    """Compress everything readable from ``src`` into ``dst``, one block at a time.

    Memory use depends on ``block_size`` and ``workers``, not on the size of the input,
    so this works for inputs of any size (see :func:`paw.compress` for the parameters).
    ``progress``, if given, is called with the number of bytes read from ``src`` and
    compressed so far, after each block and once at the end. ``extension`` and
    ``thorough`` work as for :func:`paw.compress`.

    Returns:
        The number of original bytes compressed.
    """
    check_block_size(block_size)
    stored = check_extension(extension)
    count = resolve_workers(workers, block_size)
    return write_blocks(
        _chunks(src, block_size),
        dst,
        modes=resolve_modes(modes),
        block_size=block_size,
        workers=count,
        progress=progress,
        extension=stored,
        thorough=thorough,
    )


# --- reading ------------------------------------------------------------------------


class _Source:
    """Reads the framing from a binary stream, turning premature EOF into FormatError."""

    def __init__(self, f: IO[bytes]) -> None:
        self.f = f
        self.consumed = 0

    def exact(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, in pieces of at most READ_CHUNK: a damaged header that
        claims a huge size fails at the real end of the data instead of allocating that size
        up front, and short reads (pipes, raw streams) are retried."""
        parts = []
        remaining = n
        while remaining:
            part = self.f.read(min(remaining, READ_CHUNK))
            if not part:
                raise FormatError("unexpected end of data")
            parts.append(part)
            remaining -= len(part)
        self.consumed += n
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def varint(self) -> int:
        return read_varint(lambda: self.exact(1)[0])

    def skip(self, n: int) -> None:
        if self.f.seekable():
            here = self.f.tell()
            end = self.f.seek(0, io.SEEK_END)
            if end - here < n:
                raise FormatError("unexpected end of data")
            self.f.seek(here + n)
            self.consumed += n
        else:
            while n:
                n -= len(self.exact(min(n, READ_CHUNK)))

    def at_eof(self) -> bool:
        return not self.f.read(1)


@dataclass(frozen=True)
class BlockHeader:
    original_size: int
    mode: Mode
    crc32: int
    body_size: int


@dataclass(frozen=True)
class FileHeader:
    block_size: int
    extension: str | None


def read_header(src: _Source) -> FileHeader:
    """Check magic and version; return the block size and the stored extension."""
    if src.exact(3) != MAGIC:
        raise FormatError("not a .paw file")
    version = src.exact(1)[0]
    if version != FORMAT_VERSION:
        raise FormatError(f"unsupported .paw format version {version}")
    block_size = src.varint()
    if not 1 <= block_size <= FORMAT_MAX_BLOCK_SIZE:
        raise FormatError("invalid block size")
    length = src.exact(1)[0]
    if length > MAX_EXTENSION:
        raise FormatError("stored extension too long")
    raw = src.exact(length)
    if not set(raw) <= _EXTENSION_CHARS:
        raise FormatError("stored extension has characters that are not allowed")
    extra = src.varint()  # optional data a decoder doesn't need: skipped
    if extra > MAX_HEADER_EXTRA:
        raise FormatError("optional header data too long")
    src.exact(extra)
    return FileHeader(block_size, raw.decode("ascii") or None)


def read_extension(f: IO[bytes]) -> str | None:
    """The extension stored in a .paw stream's header (reads only the header)."""
    return read_header(_Source(f)).extension


def scan_blocks(
    src: _Source, block_size: int, *, max_size: int | None = None, max_block_size: int | None = None
) -> Iterator[BlockHeader]:
    """Yield block headers up to and including validation of the end record.

    After each header the caller must consume exactly ``body_size`` bytes.
    """
    total = 0
    while True:
        n = src.varint()
        if n == 0:
            if src.varint() != total:
                raise FormatError("total size does not match the blocks")
            if not src.at_eof():
                raise FormatError("unexpected data after the end of the .paw stream")
            return
        if n > block_size:
            raise FormatError("block larger than the declared block size")
        if max_block_size is not None and n > max_block_size:
            raise SizeLimitError(f"a block holds {n:,} bytes, more than max_block_size={max_block_size:,}")
        total += n
        if max_size is not None and total > max_size:
            raise SizeLimitError(f"data decompresses to more than max_size={max_size:,} bytes")
        try:
            mode = Mode(src.exact(1)[0])
        except ValueError:
            raise FormatError("unknown block mode") from None
        crc = int.from_bytes(src.exact(4), "little")
        yield BlockHeader(n, mode, crc, src.varint())


DecodeJob = tuple[BlockHeader, bytes, int]  # header, body, .paw bytes read up to its end


def decode_block(job: DecodeJob) -> tuple[bytes, int]:
    """Decode and check one block (top level, so worker processes can run it)."""
    header, body, consumed = job
    data = decompress_block(header.mode, body, header.original_size)
    if zlib.crc32(data) != header.crc32:
        raise FormatError("checksum mismatch: the file is damaged or was written incorrectly")
    return data, consumed


def iter_decompressed(
    f: IO[bytes],
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    progress: Progress | None = None,
    workers: Workers = 1,
) -> Iterator[bytes]:
    """Decompress a .paw stream block by block, with ``workers`` blocks decoded at once."""
    src = _Source(f)
    block_size = read_header(src).block_size
    largest = min(block_size, max_block_size or block_size)
    count = resolve_workers(workers, largest, per_byte=decode_memory_per_byte())

    def jobs() -> Iterator[DecodeJob]:
        for header in scan_blocks(src, block_size, max_size=max_size, max_block_size=max_block_size):
            yield header, src.exact(header.body_size), src.consumed

    for data, consumed in ordered_map(decode_block, jobs(), count):
        if progress is not None:
            progress(consumed)
        yield data
    if progress is not None:
        progress(src.consumed)


def decompress_stream(
    src: IO[bytes],
    dst: Writable,
    *,
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    progress: Progress | None = None,
    workers: Workers = 1,
) -> int:
    """Decompress a .paw stream from ``src`` into ``dst``, one block at a time.

    ``max_size``, ``max_block_size`` and ``workers`` work as for :func:`paw.decompress`.
    ``progress``, if given, is called with the number of .paw bytes read from ``src`` so
    far, after each block and once at the end.

    Returns:
        The number of bytes written.
    """
    total = 0
    for data in iter_decompressed(src, max_size, max_block_size, progress, workers):
        dst.write(data)
        total += len(data)
    return total


# --- file object ----------------------------------------------------------------------


class PawFile(io.BufferedIOBase):
    """A binary file object that compresses on write or decompresses on read.

    Use :func:`paw.open` to create one.
    """

    def __init__(
        self,
        fileobj: IO[bytes],
        mode: str,
        *,
        close_fileobj: bool,
        block_size: int,
        modes: tuple[Mode, ...] | None,
        max_size: int | None,
        max_block_size: int | None,
        extension: str | None = None,
        thorough: bool = False,
    ) -> None:
        super().__init__()
        self._f = fileobj
        self._mode = mode
        self._close_fileobj = close_fileobj
        self._block_size = block_size
        self._modes = modes
        self._thorough = thorough
        self._buffer = bytearray()
        self._total = 0
        self._pos = 0  # read mode: offset into _buffer
        self._blocks: Iterator[bytes] | None = None
        if mode == "w":
            fileobj.write(_header(block_size, extension))
        else:
            self._blocks = iter_decompressed(fileobj, max_size, max_block_size)

    @property
    def name(self) -> str:
        """Name of the underlying file, if it has one."""
        return str(getattr(self._f, "name", ""))

    def readable(self) -> bool:
        return self._mode == "r"

    def writable(self) -> bool:
        return self._mode == "w"

    def _check(self, mode: str) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if self._mode != mode:
            raise io.UnsupportedOperation("not readable" if mode == "r" else "not writable")

    # reading
    def _fill(self, want: int) -> None:
        """Decode blocks until at least ``want`` unread bytes are buffered (or EOF)."""
        assert self._blocks is not None
        while len(self._buffer) - self._pos < want:
            block = next(self._blocks, None)
            if block is None:
                return
            del self._buffer[: self._pos]
            self._pos = 0
            self._buffer += block

    def read(self, size: int | None = -1) -> bytes:
        self._check("r")
        if size is None or size < 0:
            self._fill(1 << 62)
            size = len(self._buffer) - self._pos
        else:
            self._fill(size)
        data = bytes(self._buffer[self._pos : self._pos + size])
        self._pos += len(data)
        return data

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def peek(self, size: int = 0) -> bytes:
        self._check("r")
        self._fill(max(size, 1))
        return bytes(self._buffer[self._pos :])

    # writing
    def write(self, data: bytes | bytearray | memoryview) -> int:  # type: ignore[override]
        self._check("w")
        view = memoryview(data).cast("B")
        self._buffer += view
        while len(self._buffer) >= self._block_size:
            self._emit(bytes(self._buffer[: self._block_size]))
            del self._buffer[: self._block_size]
        return len(view)

    def _emit(self, block: bytes) -> None:
        self._f.write(encode_block((block, self._modes, self._thorough)))
        self._total += len(block)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self._mode == "w":
                if self._buffer:
                    self._emit(bytes(self._buffer))
                self._f.write(_end(self._total))
                self._f.flush()
        finally:
            try:
                if self._close_fileobj:
                    self._f.close()
            finally:
                super().close()


OpenFile = str | os.PathLike[str] | IO[bytes]
BinaryMode = Literal["r", "rb", "w", "wb", "x", "xb"]
TextMode = Literal["rt", "wt", "xt"]


# Type checkers pick the first signature that fits, so code that opens a file in a literal
# binary or text mode gets the exact type back; a mode held in a variable gets the union.
@overload
def open(
    file: OpenFile,
    mode: BinaryMode = "rb",
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    modes: Iterable[Mode | int] | None = None,
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    extension: str | None = None,
    thorough: bool = False,
    encoding: None = None,
    errors: None = None,
    newline: None = None,
) -> PawFile: ...
@overload
def open(
    file: OpenFile,
    mode: TextMode,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    modes: Iterable[Mode | int] | None = None,
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    extension: str | None = None,
    thorough: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
) -> io.TextIOWrapper: ...
@overload
def open(
    file: OpenFile,
    mode: str,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    modes: Iterable[Mode | int] | None = None,
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    extension: str | None = None,
    thorough: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
) -> PawFile | io.TextIOWrapper: ...
def open(
    file: OpenFile,
    mode: str = "rb",
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    modes: Iterable[Mode | int] | None = None,
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    extension: str | None = None,
    thorough: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
) -> PawFile | io.TextIOWrapper:
    """Open a .paw file for streaming, like :func:`gzip.open`.

    Args:
        file: A path, or an existing binary file object.
        mode: ``"rb"``/``"wb"``/``"xb"`` for bytes, or ``"rt"``/``"wt"``/``"xt"`` for text
            (``"r"``, ``"w"`` and ``"x"`` mean binary).
        block_size: Block size when writing (see :func:`paw.compress`).
        modes: Modes to try when writing (see :func:`paw.compress`).
        max_size, max_block_size: Limits when reading (see :func:`paw.decompress`).
        extension, thorough: When writing, as for :func:`paw.compress`.
        encoding, errors, newline: Text mode only, as for :func:`open`.

    Returns:
        A :class:`PawFile`, or an :class:`io.TextIOWrapper` around one in text mode.
        Data is compressed block by block as it is written, so memory use stays bounded;
        writing is single-process (use :func:`paw.compress_file` with ``workers`` for
        parallel compression).
    """
    kind = mode.replace("b", "").replace("t", "")
    if kind not in ("r", "w", "x") or ("b" in mode and "t" in mode) or len(set(mode)) != len(mode):
        raise ValueError(f"invalid mode: {mode!r}")
    text = "t" in mode
    if not text and (encoding is not None or errors is not None or newline is not None):
        raise ValueError("encoding, errors and newline are only allowed in text mode")
    check_block_size(block_size)
    resolved = resolve_modes(modes)
    stored = check_extension(extension)

    if isinstance(file, (str, os.PathLike)):
        fileobj: IO[bytes] = builtins.open(file, kind + "b")  # noqa: SIM115 - owned by PawFile
        close_fileobj = True
    elif hasattr(file, "read") or hasattr(file, "write"):
        fileobj, close_fileobj = file, False
    else:
        raise TypeError("file must be a path or a binary file object")

    binary = PawFile(
        fileobj,
        "r" if kind == "r" else "w",
        close_fileobj=close_fileobj,
        block_size=block_size,
        modes=resolved,
        max_size=max_size,
        max_block_size=max_block_size,
        extension=stored,
        thorough=thorough,
    )
    if text:
        return io.TextIOWrapper(binary, encoding=encoding, errors=errors, newline=newline)
    return binary
