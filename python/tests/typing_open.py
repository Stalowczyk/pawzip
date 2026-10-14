"""Static typing checks for paw.open, run by mypy (see [tool.mypy] in pyproject.toml).

Not a pytest module: nothing here runs. Each annotated assignment fails to type-check if
paw.open stops returning the exact type for its mode.
"""

import io
from typing import TYPE_CHECKING

import paw

if TYPE_CHECKING:
    binary: paw.PawFile = paw.open("data.bin.paw")
    written: paw.PawFile = paw.open("data.bin.paw", "wb", block_size=1 << 20)
    header: bytes = binary.read(4)
    text: io.TextIOWrapper = paw.open("notes.txt.paw", "rt", encoding="utf-8")
    line: str = text.readline()

    # A mode only known at run time gives the union, which must be narrowed.
    mode: str = "rb"
    either: paw.PawFile | io.TextIOWrapper = paw.open("data.bin.paw", mode)
    unnarrowed: paw.PawFile = paw.open("data.bin.paw", mode)  # type: ignore[assignment]
