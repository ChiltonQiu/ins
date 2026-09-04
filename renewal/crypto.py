"""Encryption at rest for the blob store.

The plaintext is hashed, so content addressing and deduplication are unchanged;
only the bytes on disk are sealed. The magic header lets an existing store be
sealed in place idempotently.

Threat model, stated plainly because it is narrower than "encrypted" suggests:
this defends against a stolen backup, a copied blob directory, and a
decommissioned disk. It does not defend against a compromised host. The key
lives in the environment beside the data, so anything that can run the
application can read every document.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"RNB1"
NONCE_BYTES = 12
KEY_BYTES = 32


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def load_key(encoded: str | None) -> bytes | None:
    """None means the store runs unencrypted. That keeps local development and
    the eval harness working without key management; the application logs a
    warning at startup and PRIVACY.md says so rather than implying encryption
    is unconditional."""
    if not encoded:
        return None
    key = base64.b64decode(encoded)
    if len(key) != KEY_BYTES:
        raise ValueError(f"blob encryption key must be {KEY_BYTES} bytes")
    return key


def is_sealed(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def seal(plaintext: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def unseal(blob: bytes, key: bytes) -> bytes:
    if not is_sealed(blob):
        raise ValueError("blob is not sealed")
    start = len(MAGIC)
    nonce = blob[start : start + NONCE_BYTES]
    return AESGCM(key).decrypt(nonce, blob[start + NONCE_BYTES :], None)
