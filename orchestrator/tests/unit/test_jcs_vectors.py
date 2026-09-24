import json
from pathlib import Path

import pytest

from surakshasetu.crypto.jcs import canonical_json, sha256_hex

VECTORS = sorted((Path(__file__).parents[3] / "content" / "testvectors" / "jcs").glob("*.json"))


def test_vector_set_is_complete() -> None:
    assert len(VECTORS) >= 10


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_vector(path: Path) -> None:
    vector = json.loads(path.read_text(encoding="utf-8"))
    assert canonical_json(vector["input"]).hex() == vector["jcs_utf8_hex"]
    assert sha256_hex(vector["input"]) == vector["sha256_hex"]
