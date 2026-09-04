"""Content-addressed blob storage.

The filesystem is dumb storage; the database is the index. A blob's name is
its own sha256, so the store is immutable by construction and identical
documents deduplicate for free.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from renewal.crypto import is_sealed, load_key, seal, unseal

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_EXT = re.compile(r"[a-z0-9]{1,8}")


logger = logging.getLogger(__name__)


class BlobNotFound(KeyError):
    """Raised when a hash has no blob behind it."""


class BlobStore:
    def __init__(self, root: Path, key: bytes | None = None) -> None:
        self.root = Path(root)
        self.key = key

    def path_for(self, sha256: str, ext: str = "pdf") -> Path:
        if not _SHA256_HEX.fullmatch(sha256):
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")
        if not _EXT.fullmatch(ext):
            raise ValueError(f"not a usable extension: {ext!r}")
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.{ext}"

    def put(self, data: bytes, ext: str = "pdf") -> str:
        """The digest is of the plaintext, so sealing changes nothing about
        content addressing or deduplication."""
        digest = hashlib.sha256(data).hexdigest()
        path = self.path_for(digest, ext)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = seal(data, self.key) if self.key else data
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        return digest

    def get(self, sha256: str, ext: str = "pdf") -> bytes:
        """Unsealed blobs still read back on a keyed store: an existing archive
        is sealed by scripts/encrypt_blobs.py, and reads must keep working
        while that runs."""
        path = self.path_for(sha256, ext)
        if not path.exists():
            raise BlobNotFound(sha256)
        raw = path.read_bytes()
        if self.key and is_sealed(raw):
            return unseal(raw, self.key)
        return raw


def store_from_settings(settings) -> BlobStore:
    """The one place a running process turns settings into a blob store.

    Building a BlobStore without the key silently disables sealing, which is
    the kind of mistake that only shows up when a stolen backup turns out to
    be readable. Going through here means every entry point gets the same
    answer, and the one case where blobs really are unencrypted says so out
    loud at startup.
    """
    key = load_key(settings.blob_encryption_key)
    if key is None:
        logger.warning(
            "BLOB_ENCRYPTION_KEY is not set: documents are stored unencrypted "
            "at %s", settings.blob_root,
        )
    return BlobStore(settings.blob_root, key=key)
