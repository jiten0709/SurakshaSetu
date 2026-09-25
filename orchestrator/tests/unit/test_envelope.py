import os

import pytest
from cryptography.exceptions import InvalidTag

from surakshasetu.crypto import envelope

DEK = os.urandom(32)
AAD = "conv.slot_value:0190a0c4-0000-7000-8000-000000000001:income"
PLAINTEXT = b"income 12 lakh"


def test_round_trip_and_blob_layout() -> None:
    blob = envelope.encrypt(DEK, PLAINTEXT, AAD)

    assert blob[:1] == b"\x01"
    assert len(blob) == 1 + 12 + len(PLAINTEXT) + 16  # version, nonce, ciphertext, tag
    assert b"lakh" not in blob
    assert envelope.decrypt(DEK, blob, AAD) == PLAINTEXT


def test_every_encryption_uses_a_fresh_nonce() -> None:
    assert envelope.encrypt(DEK, PLAINTEXT, AAD) != envelope.encrypt(DEK, PLAINTEXT, AAD)


def test_wrong_aad_or_wrong_key_fails() -> None:
    blob = envelope.encrypt(DEK, PLAINTEXT, AAD)

    with pytest.raises(InvalidTag):
        envelope.decrypt(DEK, blob, "conv.slot_value:0190a0c4-0000-7000-8000-000000000001:age")
    with pytest.raises(InvalidTag):
        envelope.decrypt(os.urandom(32), blob, AAD)


def test_any_flipped_byte_fails() -> None:
    blob = envelope.encrypt(DEK, PLAINTEXT, AAD)
    for i in range(1, len(blob)):  # the nonce, the ciphertext and the tag
        tampered = bytearray(blob)
        tampered[i] ^= 0x01
        with pytest.raises(InvalidTag):
            envelope.decrypt(DEK, bytes(tampered), AAD)


def test_unknown_version_is_refused() -> None:
    blob = envelope.encrypt(DEK, PLAINTEXT, AAD)
    with pytest.raises(ValueError, match="version"):
        envelope.decrypt(DEK, b"\x02" + blob[1:], AAD)
