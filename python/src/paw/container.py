"""Whole-buffer API: compress / decompress / info."""

from __future__ import annotations

import builtins
import io
import os
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import IO

from .blocks import Mode, resolve_modes
from .files import StrPath
from .parallel import Workers, resolve_workers
from .stream import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_BLOCK_SIZE,
    FORMAT_VERSION,
    _Source,
    check_block_size,
    check_extension,
    iter_decompressed,
    read_header,
    scan_blocks,
    write_blocks,
)

Buffer = bytes | bytearray | memoryview


@dataclass(frozen=True)
class PawInfo:
    """Facts about a .paw file, read from its headers without decompressing it."""

    original_size: int
    """Size of the decompressed data in bytes."""
    compressed_size: int
    """Size of the .paw data in bytes."""
    block_size: int
    """Maximum block size the file was written with."""
    modes: tuple[Mode, ...]
    """The encoding of each block, in order (empty for empty data)."""
    format_version: int
    """Version of the .paw format."""
    extension: str | None = None
    """Extension of the original file (without the dot), if one was stored."""

    @property
    def block_count(self) -> int:
        """Number of blocks."""
        return len(self.modes)

    @property
    def ratio(self) -> float:
        """Compressed size as a fraction of the original (1.0 for empty data)."""
        return self.compressed_size / self.original_size if self.original_size else 1.0

    @property
    def mode_summary(self) -> str:
        """The block modes as text, e.g. ``"bwt-mtf-map"`` or ``"bwt-mtf-map x3, map x1"``."""
        counts = Counter(self.modes)
        if len(counts) == 1 and self.block_count == 1:
            return self.modes[0].label
        return ", ".join(f"{m.label} x{c}" for m, c in counts.most_common()) or "-"


def compress(
    data: Buffer,
    modes: Iterable[Mode | int] | None = None,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    workers: Workers = 1,
    extension: str | None = None,
    thorough: bool = False,
) -> bytes:
    """Compress data into the .paw format.

    The data is cut into independent blocks of ``block_size`` bytes. For each block every
    suitable mode is tried and the smallest result is kept (the delta modes only when the
    block looks smooth enough for them to help).

    Memory: compressing a block needs roughly 100x its size (about 1.6 GB for a full
    16 MiB block), per worker. Smaller data needs proportionally less.

    Args:
        data: The bytes to compress (``bytes``, ``bytearray`` or ``memoryview``).
        modes: Modes to try for each block; ``None`` (default) means automatic. A single
            mode such as ``[Mode.MAP]`` is faster.
        block_size: Maximum block size in bytes (default 16 MiB). Larger blocks compress
            slightly better and need more memory. Blocks over 64 MiB can only be
            decompressed with a raised ``max_block_size`` (see :func:`decompress`).
        workers: Blocks compressed in parallel. The default of 1 is the safest; ``"auto"``
            uses one per CPU core but only as many as fit into half of the memory currently
            available. Workers are threads; only the pure-Python fallback (``PAW_PURE=1``)
            starts processes, which on Windows and macOS need code guarded by
            ``if __name__ == "__main__":``.
        extension: The original file's extension (``"png"`` or ``".png"``), stored so that
            :func:`decompress_file` can restore it whatever the .paw file is renamed to. Up
            to 16 letters, digits, ``-`` or ``_``.
        thorough: Try every mode even on blocks whose sample shows they are already
            compressed (JPEG, ZIP, ...). Such blocks are normally stored at once; this can
            still save a percent or so, at the cost of minutes per block.

    Returns:
        The complete .paw data.

    Raises:
        ValueError: ``modes`` is empty or contains an unknown mode, ``block_size`` is out
            of range, ``workers`` is invalid, or ``extension`` has characters that are not
            allowed.
    """
    data = bytes(data)
    check_block_size(block_size)
    stored = check_extension(extension)
    resolved = resolve_modes(modes)
    blocks = [data[i : i + block_size] for i in range(0, len(data), block_size)]
    count = resolve_workers(workers, min(block_size, max(len(data), 1)), len(blocks))
    out = io.BytesIO()
    write_blocks(
        blocks, out, modes=resolved, block_size=block_size, workers=count, extension=stored, thorough=thorough
    )
    return out.getvalue()


def decompress(
    blob: Buffer,
    *,
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    workers: Workers = 1,
) -> bytes:
    """Decompress .paw data and verify the checksum of every block.

    Both limits are checked from the headers before a block is decoded, so a small
    malicious file can't make this allocate large amounts of memory.

    Args:
        blob: The complete .paw data.
        max_size: Refuse data that decompresses to more than this many bytes. Use it for
            untrusted input.
        max_block_size: Refuse blocks that hold more than this many bytes (default 64 MiB),
            since decoding a block needs about 20x its size in memory. Files written with
            the default block size are never affected; for files written with larger
            blocks, pass a larger value or ``None`` for no limit.
        workers: Blocks decoded at once (only files with several blocks gain). The default
            of 1 uses no extra threads; ``"auto"`` uses one per CPU core that fits into
            half of the memory currently available.

    Returns:
        The original data, exactly as it was passed to :func:`compress`.

    Raises:
        FormatError: ``blob`` is not valid .paw data, is damaged, or fails a checksum.
        SizeLimitError: the data would be larger than ``max_size``, or a block larger than
            ``max_block_size``.
    """
    return b"".join(iter_decompressed(io.BytesIO(bytes(blob)), max_size, max_block_size, workers=workers))


def info(source: Buffer | StrPath | IO[bytes]) -> PawInfo:
    """Read the headers of .paw data without decompressing it.

    Args:
        source: The .paw data as bytes, the path of a .paw file, or a binary file object
            positioned at its start (block bodies are skipped, with seeking when the file
            supports it).

    Raises:
        FormatError: the headers are invalid or the data is truncated. Damage inside a
            block body is only detected by :func:`decompress`.
        OSError: the file can't be read.
    """
    if isinstance(source, (str, os.PathLike)):
        with builtins.open(source, "rb") as f:
            return info(f)
    if isinstance(source, Buffer):
        stream: IO[bytes] = io.BytesIO(bytes(source))
    elif hasattr(source, "read"):
        stream = source
    else:
        raise TypeError("source must be .paw bytes, a path or a binary file object")
    src = _Source(stream)
    file_header = read_header(src)
    block_size = file_header.block_size
    total = 0
    modes = []
    for header in scan_blocks(src, block_size):
        modes.append(header.mode)
        total += header.original_size
        src.skip(header.body_size)
    return PawInfo(total, src.consumed, block_size, tuple(modes), FORMAT_VERSION, file_header.extension)
