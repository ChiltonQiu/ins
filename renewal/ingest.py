from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.models import Document
from renewal.pdftext import read_pdf

logger = logging.getLogger(__name__)


def ingest_pdf(
    session: Session,
    store: BlobStore,
    *,
    data: bytes,
    original_filename: str,
    doc_type: str = "dec_page",
    source: str = "manual_upload",
    agency_id: int | None = None,
    inbound_message_id: int | None = None,
) -> Document:
    """Store bytes and record a document row.

    Identical bytes deduplicate to one blob; each upload still gets its own
    document row, because the same PDF arriving twice is two events.

    Provenance is passed in rather than set on the returned row, so the values
    land in the INSERT. Assigning them afterwards would issue an UPDATE, which
    no application code in this system does.
    """
    digest = store.put(data)
    info = read_pdf(data)
    document = Document(
        blob_sha256=digest,
        original_filename=original_filename,
        page_count=info.page_count,
        has_text_layer=info.has_text_layer,
        doc_type=doc_type,
        source=source,
        agency_id=agency_id,
        inbound_message_id=inbound_message_id,
    )
    session.add(document)
    session.flush()
    # Ids and hashes only. Never log document content.
    logger.info(
        "ingested document id=%s sha256=%s pages=%s text_layer=%s",
        document.id,
        digest,
        info.page_count,
        info.has_text_layer,
    )
    return document
