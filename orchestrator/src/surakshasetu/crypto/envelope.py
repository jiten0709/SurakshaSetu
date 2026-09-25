"""AES-256-GCM for every personal field at rest, under the subject's DEK.

Blob layout: b"\\x01" (format version) + nonce (12 bytes) + ciphertext with its 16-byte tag.

The AAD binds a ciphertext to where it lives, so a blob copied to another row fails to decrypt:
- conv.turn text: "conv.turn:<turn_id>"
- conv.slot_value value: "conv.slot_value:<session_id>:<slot>"
- audit payload: "audit:<session_id>:<seq>"
- a wrapped DEK in keyvault: its key_ref
"""

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"\x01"
NONCE_BYTES = 12


def encrypt(dek: bytes, plaintext: bytes, aad: str) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return VERSION + nonce + AESGCM(dek).encrypt(nonce, plaintext, aad.encode())


def decrypt(dek: bytes, blob: bytes, aad: str) -> bytes:
    """Raises cryptography's InvalidTag on a wrong key, a wrong AAD or any tampered byte."""
    if blob[:1] != VERSION:
        raise ValueError("unknown envelope version")
    nonce, ciphertext = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
    return AESGCM(dek).decrypt(nonce, ciphertext, aad.encode())
