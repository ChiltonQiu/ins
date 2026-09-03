"""Per-page text for every document.

This path has no judgment in it and cannot be silently wrong in a harmful way.
Search and date extraction read from here; neither depends on field-extraction
accuracy, and nothing in this module may couple them.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.models import Document, DocumentText
from renewal.pdftext import MIN_CHARS_FOR_TEXT_LAYER, rasterize_page, read_pdf
from renewal.text.ocr import ocr_page

logger = logging.getLogger(__name__)

TEXT_VERSION = "text-v1"


def has_text(session: Session, document_id: int, version: str = TEXT_VERSION) -> bool:
    return session.scalar(
        select(DocumentText.id)
        .where(DocumentText.document_id == document_id)
        .where(DocumentText.extractor_version == version)
        .limit(1)
    ) is not None


def page_text(
    session: Session, document_id: int, page_number: int, version: str = TEXT_VERSION
) -> str:
    return session.scalar(
        select(DocumentText.text)
        .where(DocumentText.document_id == document_id)
        .where(DocumentText.page_number == page_number)
        .where(DocumentText.extractor_version == version)
    ) or ""


def extract_text(
    session: Session,
    store: BlobStore,
    document: Document,
    *,
    version: str = TEXT_VERSION,
) -> list[DocumentText]:
    """Idempotent at a given version, so the bulk-import stage can be re-run
    over the whole archive without duplicating work.

    The method is decided per page: a PDF can carry a digital dec page and a
    scanned endorsement, and search quality varies by which produced the text.

    A page always gets a row, empty or not: a row means "processed", and its
    absence means "not yet processed". A blank page falls through to OCR, which
    returns an empty string, and the row is written either way.
    """
    if has_text(session, document.id, version):
        return list(
            session.scalars(
                select(DocumentText)
                .where(DocumentText.document_id == document.id)
                .where(DocumentText.extractor_version == version)
                .order_by(DocumentText.page_number)
            )
        )

    data = store.get(document.blob_sha256)
    pdf = read_pdf(data)
    rows: list[DocumentText] = []
    for page in pdf.pages:
        text = page.text
        method = "pymupdf"
        if len("".join(text.split())) < MIN_CHARS_FOR_TEXT_LAYER:
            text = ocr_page(rasterize_page(data, page.page_number))
            method = "ocr_tesseract"
        row = DocumentText(
            document_id=document.id,
            page_number=page.page_number,
            text=text,
            extraction_method=method,
            extractor_version=version,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    # Counts and methods only. Never the text.
    logger.info(
        "text extracted document_id=%s sha256=%s pages=%s methods=%s version=%s",
        document.id,
        document.blob_sha256,
        len(rows),
        sorted({r.extraction_method for r in rows}),
        version,
    )
    return rows
