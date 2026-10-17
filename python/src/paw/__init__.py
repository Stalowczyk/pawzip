"""pawzip: a position-map + series compressor that writes .paw files.

Quick start::

    import paw

    blob = paw.compress(data)          # bytes -> .paw bytes
    data = paw.decompress(blob)        # .paw bytes -> original bytes
    paw.info(blob).original_size       # read the headers without decompressing

    paw.compress_file("notes.txt")     # writes notes.paw, streaming block by block
    paw.decompress_file("notes.paw")   # writes notes.txt: the extension is stored

    with paw.open("log.paw", "wt") as f:       # like gzip.open
        f.write("hello\\n")

For untrusted input pass ``max_size`` when decompressing; blocks larger than 64 MiB are
refused unless ``max_block_size`` allows them. Everything listed in ``__all__`` is the
public API; submodules are internal.
"""

from .blocks import Mode
from .container import PawInfo, compress, decompress, info
from .errors import FormatError, PawError, SizeLimitError
from .files import compress_file, decompress_file
from .stream import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_BLOCK_SIZE,
    FORMAT_VERSION,
    PawFile,
    compress_stream,
    decompress_stream,
    open,
)

__version__ = "1.0.1"

__all__ = [
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_MAX_BLOCK_SIZE",
    "FORMAT_VERSION",
    "FormatError",
    "Mode",
    "PawError",
    "PawFile",
    "PawInfo",
    "SizeLimitError",
    "__version__",
    "compress",
    "compress_file",
    "compress_stream",
    "decompress",
    "decompress_file",
    "decompress_stream",
    "info",
    "open",
]
