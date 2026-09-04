"""Walk a directory tree and put every PDF through the ingest pipeline.

Safely re-runnable. Content addressing makes a re-import free: a blob already
in the store is not rewritten, and a document whose bytes are already known at
this path is skipped rather than duplicated.

One document failing never stops the walk. Import is cheap and cannot be done
retroactively for a document that is later deleted from the source tree, so
getting the archive in matters more than getting every page perfect on the
first pass — extraction is a pure function of (blob, extractor_version) and
can be re-run.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore, store_from_settings
from renewal.config import Settings, load_settings
from renewal.db import session_scope
from renewal.models import Document
from renewal.pipeline import ingest_document, run_dates_stage, run_text_stage
from renewal.providers import ModelClient, build_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportStats:
    seen: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0


def walk_pdfs(root: Path) -> Iterator[Path]:
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.suffix.lower() == ".pdf":
            yield path


def import_tree(
    session: Session,
    store: BlobStore,
    root: Path,
    *,
    agency_id: int,
    client: ModelClient | None = None,
    settings: Settings | None = None,
) -> ImportStats:
    seen = imported = skipped = failed = 0
    for path in walk_pdfs(root):
        seen += 1
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            existing = session.scalar(
                select(Document.id).where(Document.blob_sha256 == digest).limit(1)
            )
            if existing is not None:
                # Already imported. Re-run the text stage in case it is behind
                # the current version; it is a no-op when it is not.
                document = session.get(Document, existing)
                run_text_stage(session, store, document)
                run_dates_stage(session, document, client=client, settings=settings)
                skipped += 1
                continue
            ingest_document(
                session, store, data=data, original_filename=path.name,
                source="bulk_import", agency_id=agency_id,
                model_client=client, settings=settings,
            )
            imported += 1
        except Exception:  # noqa: BLE001 - one bad file must not stop the archive
            failed += 1
            # Path only. Never the contents.
            logger.exception("import failed path=%s", path)
    return ImportStats(seen=seen, imported=imported, skipped=skipped, failed=failed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--agency-id", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    store = store_from_settings(settings)
    with session_scope() as session:
        stats = import_tree(
            session, store, args.root, agency_id=args.agency_id,
            client=build_client(settings), settings=settings,
        )
    print(f"seen {stats.seen}, imported {stats.imported}, "
          f"skipped {stats.skipped}, failed {stats.failed}")


if __name__ == "__main__":
    main()
