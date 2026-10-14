"""Exceptions raised by pawzip."""


class PawError(ValueError):
    """Base class for all pawzip errors. Catch this to handle any of them."""


class FormatError(PawError):
    """The input is not a valid .paw file: wrong magic, unsupported version, damaged or
    truncated data, or a checksum mismatch."""


class SizeLimitError(PawError):
    """The file would decompress to more bytes than allowed (`max_size`), or holds a block
    larger than allowed (`max_block_size`, 64 MiB by default)."""
