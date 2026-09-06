"""Password hashing.

scrypt from the standard library rather than a KDF from a dependency: it is
memory-hard, and this project's dependency list is deliberately short.

The encoded form carries its own parameters, so cost can be raised later
without invalidating every existing hash. A hash that verifies under old
parameters is rewritten at the next login, which is the only moment the
plaintext is available.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
_SALT_BYTES = 16
_DKLEN = 32

# scrypt at n=2**15, r=8 needs 32 MiB, and OpenSSL's default ceiling rejects
# exactly that with "memory limit exceeded". The limit has to be raised
# explicitly or the call never succeeds.
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=_DKLEN, maxmem=_MAXMEM,
    )


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    digest = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join([
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        _b64(salt), _b64(digest),
    ])


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """None for anything that is not a hash this module wrote. A corrupt row
    must fail the login rather than raise out of it."""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
    except ValueError:
        return None
    if scheme != "scrypt":
        return None
    try:
        return int(n), int(r), int(p), _unb64(salt), _unb64(digest)
    except (ValueError, TypeError):
        return None


def verify_password(password: str, encoded: str) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, digest = parsed
    try:
        candidate = _derive(password, salt, n, r, p)
    except (ValueError, OverflowError):
        # Parameters outside what scrypt accepts, e.g. an n that is not a
        # power of two, or an oversized integer. Same answer as a wrong password.
        return False
    return hmac.compare_digest(candidate, digest)


def needs_rehash(encoded: str) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, _, _ = parsed
    return (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


# Verified against when no account matches, so an unknown address costs the
# same wall-clock time as a wrong password. Nobody knows this plaintext.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
