"""Optional parallel block coding, sized so it never exhausts memory.

With the Rust core the workers are threads (it releases the GIL while it codes a block);
the pure-Python reference uses worker processes instead.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Literal, TypeVar

from . import blocks

T = TypeVar("T")
R = TypeVar("R")

Workers = int | Literal["auto"]

# Measured peak memory per block byte. Compressing: up to ~93x in pure Python and ~89x with
# the Rust core (which tries several modes at once). Decompressing: ~20x and ~6.5x.
MEMORY_PER_BLOCK_BYTE = 100
DECODE_MEMORY_PER_BLOCK_BYTE = 20
NATIVE_DECODE_MEMORY_PER_BLOCK_BYTE = 8
# "auto" uses at most this fraction of the memory that is currently available.
MEMORY_SHARE = 0.5


def available_memory() -> int | None:
    """Bytes of physical memory currently available, or ``None`` if unknown."""
    try:
        if sys.platform == "win32":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
            return None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, AttributeError):
        pass
    return None


def resolve_workers(
    workers: Workers, block_size: int, blocks: int | None = None, per_byte: int = MEMORY_PER_BLOCK_BYTE
) -> int:
    """Turn a ``workers`` argument into a process count.

    ``"auto"`` uses one worker per CPU core, but no more than fit into half of the memory
    that is available right now (each worker needs ``per_byte`` times the block size); if the
    available memory can't be determined it uses a single worker.
    """
    if workers == "auto":
        free = available_memory()
        if free is None:
            count = 1
        else:
            per_worker = per_byte * block_size
            count = min(os.cpu_count() or 1, int(free * MEMORY_SHARE) // per_worker)
    elif isinstance(workers, int) and not isinstance(workers, bool) and workers >= 1:
        count = workers
    else:
        raise ValueError(f"workers must be a positive integer or 'auto', not {workers!r}")
    if blocks is not None:
        count = min(count, blocks)
    return max(1, count)


def decode_memory_per_byte() -> int:
    """Peak memory per block byte while decompressing, for :func:`resolve_workers`."""
    return NATIVE_DECODE_MEMORY_PER_BLOCK_BYTE if blocks.NATIVE else DECODE_MEMORY_PER_BLOCK_BYTE


def ordered_map(func: Callable[[T], R], items: Iterable[T], workers: int) -> Iterator[R]:
    """Like ``map(func, items)``, optionally in worker threads or processes, keeping the order.

    Items are pulled from ``items`` lazily and at most ``workers + 1`` are in flight, so
    memory stays bounded even for an endless input stream. Errors come out in order too: if
    pulling the next item fails, the results already in flight are delivered (or raise)
    first, exactly as with a single worker.
    """
    if workers <= 1:
        yield from map(func, items)
        return
    pool: Executor = ThreadPoolExecutor(workers) if blocks.NATIVE else ProcessPoolExecutor(workers)
    with pool:
        pending: deque[Future[R]] = deque()
        source = iter(items)
        while True:
            try:
                item = next(source)
            except StopIteration:
                break
            except BaseException:
                while pending:
                    yield pending.popleft().result()
                raise
            pending.append(pool.submit(func, item))
            if len(pending) > workers:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()
