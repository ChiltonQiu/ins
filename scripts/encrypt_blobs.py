"""Seal an existing blob store in place. Idempotent: the magic header records
what is already done, so a re-run after an interrupted pass is free.

Writes through a temporary file and replaces atomically, so an interruption
leaves either the old blob or the new one, never a truncated file.
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

from renewal.config import load_settings
from renewal.crypto import is_sealed, load_key, seal

logger = logging.getLogger(__name__)

# A real blob's filename is exactly {64 lowercase hex}.{ext}, per
# BlobStore.path_for (renewal/blobstore.py). Every scratch file this script or
# BlobStore.put ever writes has an extra ".tmp" appended to that name, giving
# it two dots (e.g. "<hex>.pdf.tmp" or "<hex>.tmp.tmp") -- which never matches
# this single-dot pattern. So this filter, on its own, both skips the script's
# own scratch files and includes a legitimate blob stored with ext="tmp".
# Do not also skip on path.suffix == ".tmp": that would silently exclude a
# real "<hex>.tmp" blob again.
_BLOB_NAME = re.compile(r"[0-9a-f]{64}\.[a-z0-9]{1,8}")


def seal_store(root: Path, key: bytes) -> tuple[int, int]:
    sealed = skipped = 0
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or not _BLOB_NAME.fullmatch(path.name):
            continue
        raw = path.read_bytes()
        if is_sealed(raw):
            skipped += 1
            continue
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(seal(raw, key))
        tmp.replace(path)
        sealed += 1
        # Path only. Never the contents.
        logger.info("sealed %s", path.name)
    return sealed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()
    settings = load_settings()
    key = load_key(settings.blob_encryption_key)
    if key is None:
        raise SystemExit("BLOB_ENCRYPTION_KEY is not set; nothing to do")
    sealed, skipped = seal_store(args.root or settings.blob_root, key)
    print(f"sealed {sealed}, already sealed {skipped}")


if __name__ == "__main__":
    main()
