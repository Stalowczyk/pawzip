"""Run the Rust `paw` binary and the Python `paw` command through the same scenarios and
check that they behave the same: exit status, files written, stdout and stderr.

    cargo build --release -p pawzip-cli && python rust/cli_parity.py

Timings ("0.3s") are normalised; usage errors (exit status 2) only compare the status,
since clap and argparse word them differently. Needs the dev environment.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUST = Path(__file__).resolve().parent
EXE = RUST / "target" / "release" / ("paw.exe" if sys.platform == "win32" else "paw")
PYTHON = [sys.executable, "-m", "paw"]

TEXT = b"the map, the series and the gaps.\n" * 2000

SCENARIOS = [
    # (description, setup files, commands run one after another in the same directory)
    (
        "compress and back",
        {"notes.txt": TEXT},
        [["compress", "notes.txt"], ["decompress", "-f", "notes.paw"]],
    ),
    ("refuses to overwrite", {"notes.txt": TEXT}, [["compress", "notes.txt"], ["compress", "notes.txt"]]),
    (
        "renamed file",
        {"notes.txt": TEXT},
        [["compress", "notes.txt"], ["decompress", "-o", "x.paw", "notes.paw"]],
    ),
    (
        "output name",
        {"notes.txt": TEXT},
        [["compress", "-o", "out.bin", "notes.txt"], ["decompress", "-o", "back.txt", "out.bin"]],
    ),
    (
        "no suffix",
        {"plain": TEXT},
        [["compress", "plain"], ["decompress", "plain.paw"], ["decompress", "plain"]],
    ),
    ("to stdout", {"notes.txt": TEXT}, [["compress", "-c", "notes.txt"]]),
    (
        "modes and blocks",
        {"notes.txt": TEXT},
        [["compress", "-m", "map", "-m", "2", "-B", "10K", "-q", "notes.txt"], ["info", "notes.paw"]],
    ),
    (
        "several files",
        {"a.txt": TEXT, "b.dat": TEXT[:999], "empty": b""},
        [
            ["compress", "a.txt", "b.dat", "empty"],
            ["test", "a.paw", "b.paw", "empty.paw"],
            ["info", "empty.paw"],
        ],
    ),
    (
        "damaged",
        {"notes.txt": TEXT},
        [
            ["compress", "-q", "notes.txt"],
            ["corrupt", "notes.paw"],
            ["test", "notes.paw"],
            ["decompress", "notes.paw"],
        ],
    ),
    (
        "limits",
        {"notes.txt": TEXT},
        [
            ["compress", "-q", "-B", "4K", "notes.txt"],
            ["test", "--max-size", "10K", "notes.paw"],
            ["test", "--max-block-size", "1K", "notes.paw"],
            ["decompress", "-c", "-T", "3", "notes.paw"],
        ],
    ),
    ("missing input", {}, [["compress", "nope.txt"], ["info", "nope.paw"]]),
    ("not a paw file", {"fake.paw": b"PKzip!"}, [["test", "fake.paw"], ["info", "fake.paw"]]),
    (
        "usage errors",
        {"a": b"x", "b": b"y"},
        [
            ["compress", "-o", "x", "a", "b"],
            ["compress", "-c", "a", "b"],
            ["compress", "-B", "0", "a"],
            ["compress", "-T", "x", "a"],
            ["compress", "-m", "zip", "a"],
        ],
    ),
]


def normalise(text: str) -> str:
    text = text.replace("\r\n", "\n")  # Python writes CRLF on Windows
    text = re.sub(r"\d+\.\ds\b", "N.Ns", text)
    # OS error texts differ between Python and Rust: keep only that it was an OS error
    text = re.sub(r"(paw: [^:\n]+: ).*(No such file|cannot find).*", r"\1<not found>", text)
    return text


def run(command: list[str], cwd: Path, rust: bool) -> tuple[int, str, str, bytes]:
    if command[0] == "corrupt":  # flip a byte in the middle of the file
        f = cwd / command[1]
        data = bytearray(f.read_bytes())
        data[len(data) // 2] ^= 0x40
        f.write_bytes(bytes(data))
        return 0, "", "", b""
    argv = [str(EXE), *command] if rust else [*PYTHON, *command]
    p = subprocess.run(argv, cwd=cwd, capture_output=True)
    stdout = p.stdout if "-c" in command else b""
    text = "" if "-c" in command else p.stdout.decode()
    return p.returncode, normalise(text), normalise(p.stderr.decode()), stdout


def snapshot(d: Path) -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in sorted(d.iterdir()) if f.is_file()}


def main() -> int:
    failures = 0
    for description, files, commands in SCENARIOS:
        results = []
        for rust in (False, True):
            d = Path(tempfile.mkdtemp())
            for name, data in files.items():
                (d / name).write_bytes(data)
            outcome = [run(c, d, rust) for c in commands]
            results.append((outcome, snapshot(d)))
            shutil.rmtree(d)
        (py, py_files), (rs, rs_files) = results
        problems = []
        for command, a, b in zip(commands, py, rs, strict=True):
            if a[0] != b[0]:
                problems.append(f"{command}: exit {a[0]} (Python) vs {b[0]} (Rust)")
            elif a[0] != 2 and a[1:] != b[1:]:
                problems.append(f"{command}:\n  Python {a[1:3]}\n  Rust   {b[1:3]}")
        if py_files != rs_files:
            problems.append(f"files differ: {sorted(py_files)} vs {sorted(rs_files)}")
        print(f"{'ok  ' if not problems else 'FAIL'} {description}")
        for p in problems:
            print("     " + p)
        failures += bool(problems)
    print(f"{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios behave the same")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
