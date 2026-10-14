"""Compressing and decompressing files on disk, streaming block by block."""

from __future__ import annotations

import builtins
import os
import secrets
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import IO

from .blocks import Mode
from .parallel import Workers
from .stream import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_BLOCK_SIZE,
    Progress,
    check_extension,
    compress_stream,
    decompress_stream,
    read_extension,
)

SUFFIX = ".paw"

StrPath = str | os.PathLike[str]


def compressed_name(src: Path) -> tuple[Path, str | None]:
    """The default .paw name for ``src`` and the extension to store in it.

    ``photo.png`` -> (``photo.paw``, ``"png"``). Files without a storable extension, and
    .paw files themselves, keep their whole name: ``notes`` -> ``notes.paw``.
    """
    if src.suffix and src.suffix.lower() != SUFFIX:
        try:
            extension = check_extension(src.suffix)
        except ValueError:
            extension = None
        if extension:
            return src.with_suffix(SUFFIX), extension
    return src.with_name(src.name + SUFFIX), None


def decompressed_name(src: Path, extension: str | None) -> Path | None:
    """The default output name for the .paw file ``src`` holding ``extension``.

    ``abc.paw`` holding "png" -> ``abc.png``, whatever the .paw file was renamed to;
    ``photo.png.paw`` (gzip-style naming) -> ``photo.png``. None if there is nothing to go on.
    """
    if src.suffix.lower() == SUFFIX:
        base = src.stem
    elif extension:
        base = src.name
    else:
        return None
    if extension and not base.lower().endswith("." + extension.lower()):
        base += "." + extension
    return src.with_name(base)


def _create_temp(path: Path) -> tuple[int, Path]:
    """Create a new, empty file next to ``path``. It gets the permissions a plain ``open()``
    would give (tempfile.mkstemp would make it readable by its owner only)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            return os.open(tmp, flags, 0o666), tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create a temporary file next to {path}")


def write_atomically(path: Path, produce: Callable[[IO[bytes]], object], overwrite: bool) -> None:
    """Let ``produce`` write into a temporary file next to ``path``, then move it into
    place, so a failure never leaves a partial output file behind."""
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (pass overwrite=True to replace it)")
    fd, tmp = _create_temp(path)
    try:
        with os.fdopen(fd, "wb") as f:
            produce(f)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def compress_file(
    src: StrPath,
    dst: StrPath | None = None,
    *,
    overwrite: bool = False,
    modes: Iterable[Mode | int] | None = None,
    block_size: int = DEFAULT_BLOCK_SIZE,
    workers: Workers = 1,
    progress: Progress | None = None,
    thorough: bool = False,
) -> Path:
    """Compress a file into a .paw file.

    The file is read and compressed one block at a time, so files of any size work;
    memory use depends on ``block_size`` and ``workers`` (see :func:`paw.compress`).

    Args:
        src: The file to compress.
        dst: Where to write the result; defaults to ``src`` with its extension replaced by
            ``.paw`` (``photo.png`` -> ``photo.paw``). The extension is stored in the file,
            so :func:`decompress_file` restores it even if the .paw file is renamed.
        overwrite: Replace ``dst`` if it already exists.
        modes, block_size, workers, thorough: As for :func:`paw.compress`.
        progress: Called with the number of bytes of ``src`` compressed so far, after each
            block and once at the end (for example to drive a progress bar).

    Returns:
        The path of the written .paw file.

    Raises:
        FileExistsError: ``dst`` exists and ``overwrite`` is false.
        OSError: ``src`` can't be read or ``dst`` can't be written.
    """
    src = Path(src)
    default, extension = compressed_name(src)
    dst = Path(dst) if dst is not None else default
    with builtins.open(src, "rb") as f:
        write_atomically(
            dst,
            lambda out: compress_stream(
                f,
                out,
                modes=modes,
                block_size=block_size,
                workers=workers,
                progress=progress,
                extension=extension,
                thorough=thorough,
            ),
            overwrite,
        )
    return dst


def decompress_file(
    src: StrPath,
    dst: StrPath | None = None,
    *,
    overwrite: bool = False,
    max_size: int | None = None,
    max_block_size: int | None = DEFAULT_MAX_BLOCK_SIZE,
    progress: Progress | None = None,
    workers: Workers = 1,
) -> Path:
    """Decompress a .paw file, one block at a time.

    Args:
        src: The .paw file.
        dst: Where to write the result. By default the name of ``src`` with ``.paw``
            replaced by the stored extension (``abc.paw`` holding "png" -> ``abc.png``), or
            just without ``.paw`` if none is stored.
        overwrite: Replace ``dst`` if it already exists.
        max_size, max_block_size, workers: As for :func:`paw.decompress`.
        progress: Called with the number of bytes of ``src`` decompressed so far, after
            each block and once at the end.

    Returns:
        The path of the written file.

    Raises:
        ValueError: ``dst`` is not given, ``src`` has no ``.paw`` suffix and no stored
            extension.
        FileExistsError: ``dst`` exists and ``overwrite`` is false.
        FormatError: ``src`` is not a valid .paw file.
        SizeLimitError: the result would be larger than ``max_size``, or a block larger
            than ``max_block_size``.
        OSError: ``src`` can't be read or ``dst`` can't be written.
    """
    src = Path(src)
    with builtins.open(src, "rb") as f:
        if dst is None:
            dst = decompressed_name(src, read_extension(f))
            if dst is None:
                raise ValueError(f"{src} has no {SUFFIX} suffix; pass dst explicitly")
            f.seek(0)
        dst = Path(dst)
        write_atomically(
            dst,
            lambda out: decompress_stream(
                f, out, max_size=max_size, max_block_size=max_block_size, progress=progress, workers=workers
            ),
            overwrite,
        )
    return dst
