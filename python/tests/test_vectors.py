"""The shared test vectors in spec/vectors must decode exactly, and encode unchanged; the
hand-made ones in unusual/ must decode and those in invalid/ must be rejected."""

import hashlib
import json
from pathlib import Path

import pytest

from paw import FormatError, blocks, compress, decompress, info

VECTORS = Path(__file__).resolve().parents[2] / "spec" / "vectors"
MANIFEST = json.loads((VECTORS / "manifest.json").read_text())
UNUSUAL = json.loads((VECTORS / "unusual" / "manifest.json").read_text())
INVALID = json.loads((VECTORS / "invalid" / "manifest.json").read_text())


@pytest.fixture(params=["native", "pure"])
def decoder(request, monkeypatch):
    """Run a test with the Rust core and with the pure-Python decoder."""
    if request.param == "native" and not blocks.NATIVE:
        pytest.skip("built without the Rust core")
    if request.param == "pure":
        monkeypatch.setattr(blocks, "_core", None)
    return request.param


@pytest.mark.parametrize("vector", MANIFEST, ids=lambda v: v["paw"])
def test_decodes(vector):
    blob = (VECTORS / vector["paw"]).read_bytes()
    data = decompress(blob)
    assert len(data) == vector["size"]
    assert hashlib.sha256(data).hexdigest() == vector["sha256"]
    assert set(info(blob).modes) <= {vector["mode"]}


@pytest.mark.parametrize("vector", MANIFEST, ids=lambda v: v["paw"])
def test_encoder_is_stable(vector):
    data = (VECTORS / vector["input"]).read_bytes()
    expected = (VECTORS / vector["paw"]).read_bytes()
    assert compress(data, modes=(vector["mode"],), block_size=vector["block_size"]) == expected, (
        "encoder output changed - if intentional, rerun python/scripts/make_vectors.py"
    )


@pytest.mark.parametrize("vector", UNUSUAL, ids=lambda v: v["paw"])
def test_unusual_files_decode(vector, decoder):
    blob = (VECTORS / "unusual" / vector["paw"]).read_bytes()
    data = decompress(blob)
    assert hashlib.sha256(data).hexdigest() == vector["sha256"]
    assert data == (VECTORS / "unusual" / vector["input"]).read_bytes()


@pytest.mark.parametrize("vector", INVALID, ids=lambda v: v["paw"])
def test_invalid_files_are_rejected(vector, decoder):
    blob = (VECTORS / "invalid" / vector["paw"]).read_bytes()
    with pytest.raises(FormatError):
        decompress(blob)
