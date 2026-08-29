"""Re-run an extractor version over every document in the corpus.

Usage: python scripts/reextract.py v2

Writes new extraction rows. Nothing existing is touched, so old and new results
can be compared afterwards.
"""

from __future__ import annotations

import sys

from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.db import session_scope
from renewal.extract.runner import extract
from renewal.models import Document
from renewal.providers import build_client


def main(version: str) -> None:
    settings = load_settings()
    store = BlobStore(settings.blob_root)
    client = build_client(settings)
    with session_scope() as session:
        documents = session.query(Document).order_by(Document.id).all()
        for document in documents:
            extraction = extract(
                session, store, document, version, client=client, settings=settings
            )
            print(f"document {document.id} -> extraction {extraction.id} "
                  f"({extraction.status})")


if __name__ == "__main__":
    main(sys.argv[1])
