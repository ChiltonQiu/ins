"""The one way a document enters the system.

Bulk import, manual upload, and email intake all call ingest_document. Every
document is stored, text-extracted, date-extracted, and made searchable —
those stages have no judgment in them. Only some documents go on to structured
field extraction, and only high-confidence extractions are promoted. Search and
the calendar never depend on field-extraction accuracy.

Every stage after storage is best-effort and separately re-runnable. A stage
that raises is logged and skipped: losing the document because OCR failed would
be worse than a document with no text yet, and the stage can be re-run.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from sqlalchemy import select

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.dates.service import extract_dates
from renewal.ingest import ingest_pdf
from renewal.models import Document, DocumentText
from renewal.pdftext import PageText
from renewal.providers import ModelClient
from renewal.resolve.service import resolve_document
from renewal.text.store import TEXT_VERSION, extract_text, has_text

logger = logging.getLogger(__name__)

SOURCES = ("bulk_import", "manual_upload", "email_attachment", "email_body")


def run_text_stage(session: Session, store: BlobStore, document: Document) -> None:
    if has_text(session, document.id):
        return
    try:
        extract_text(session, store, document)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception(
            "text stage failed document_id=%s sha256=%s",
            document.id,
            document.blob_sha256,
        )


def run_resolve_stage(session: Session, document: Document) -> None:
    try:
        resolve_document(session, document)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("resolve stage failed document_id=%s", document.id)


def stored_pages(session: Session, document_id: int) -> list[PageText]:
    """Read the text back out of DocumentText rather than out of the PDF.

    That is what makes date extraction work identically for an email body,
    which has no PDF behind it at all."""
    rows = session.execute(
        select(DocumentText.page_number, DocumentText.text)
        .where(DocumentText.document_id == document_id)
        .where(DocumentText.extractor_version == TEXT_VERSION)
        .order_by(DocumentText.page_number)
    ).all()
    return [PageText(page_number, text) for page_number, text in rows]


def run_dates_stage(
    session: Session,
    document: Document,
    *,
    client: ModelClient | None = None,
    settings: Settings | None = None,
) -> None:
    try:
        extract_dates(
            session, document, stored_pages(session, document.id),
            client=client, settings=settings,
        )
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("dates stage failed document_id=%s", document.id)


def ingest_document(
    session: Session,
    store: BlobStore,
    *,
    data: bytes,
    original_filename: str,
    source: str,
    agency_id: int,
    inbound_message_id: int | None = None,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
) -> Document:
    if source not in SOURCES:
        raise ValueError(f"unknown document source: {source!r}")
    document = ingest_pdf(
        session,
        store,
        data=data,
        original_filename=original_filename,
        source=source,
        agency_id=agency_id,
        inbound_message_id=inbound_message_id,
    )
    run_text_stage(session, store, document)
    run_resolve_stage(session, document)
    # Optional rather than required: with no model configured the regex pass
    # still runs, and a caller with no model still gets the recall floor
    # instead of no dates at all.
    run_dates_stage(session, document, client=model_client, settings=settings)
    return document
