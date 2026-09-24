"""RFC 8785 JSON Canonicalization Scheme: the only way this tier hashes JSON."""

import hashlib
from typing import Any

import rfc8785


def canonical_json(obj: Any) -> bytes:
    return rfc8785.dumps(obj)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()
