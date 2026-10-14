"""The Python examples in the READMEs must run as written (python/README.md is the PyPI page)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# the top-level READMEs, plus any README one folder down that has Python examples
OTHERS = [p for p in sorted(ROOT.glob("*/README.md")) if "```python" in p.read_text(encoding="utf-8")]
READMES = [ROOT / "README.md", *OTHERS]  # OTHERS includes python/README.md


def python_blocks(path):
    return re.findall(r"```python\n(.*?)```", path.read_text(encoding="utf-8"), flags=re.DOTALL)


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_examples_run(readme, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("some notes\n" * 100)
    blocks = python_blocks(readme)
    assert blocks
    for block in blocks:
        exec(compile(block, str(readme), "exec"), {})
