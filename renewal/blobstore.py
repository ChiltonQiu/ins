"""Content-addressed blob storage.

The filesystem is dumb storage; the database is the index. A blob's name is
its own sha256, so the store is immutable by construction and identical
documents deduplicate for free.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class BlobNotFound(KeyError):
    """Raised when a hash has no blob behind it."""


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.pdf"

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.path_for(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return digest

    def get(self, sha256: str) -> bytes:
        path = self.path_for(sha256)
        if not path.exists():
            raise BlobNotFound(sha256)
        return path.read_bytes()
