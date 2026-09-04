"""Validating and storing extracted dates, and recording her judgment on them.

A date whose cited source text is not on the page it names is rejected, not
stored at low confidence. Field extraction keeps unverifiable fields because an
unverifiable field is evidence about the extractor; a date is different,
because a date renders on a calendar as a claim, and a wrong
cancellation-effective date shown confidently is the worst thing this system
can do.

Both passes' rows are kept. The calendar collapses them for display; storage
keeps them apart so their recall can be measured separately.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.dates.llm_pass import LLM_VERSION, LlmDate, find_dates_llm
from renewal.dates.regex_pass import REGEX_VERSION, find_dates
from renewal.models import DateEvent, Document, DocumentDate
from renewal.pdftext import PageText
from renewal.providers import ModelClient

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _on_page(text: str, pages: list[PageText], page_number: int) -> bool:
    if not 1 <= page_number <= len(pages):
        return False
    if not _normalize(text):
        return False
    return _normalize(text) in _normalize(pages[page_number - 1].text)


def _already_extracted(session: Session, document_id: int, version: str) -> bool:
    return session.scalar(
        select(DocumentDate.id)
        .where(DocumentDate.document_id == document_id)
        .where(DocumentDate.extractor_version == version)
        .limit(1)
    ) is not None


def _store_llm_date(
    session: Session, document_id: int, item: LlmDate, pages: list[PageText]
) -> DocumentDate | None:
    if not _on_page(item.source_text, pages, item.source_page):
        logger.info(
            "date rejected document_id=%s page=%s reason=source_text_not_on_page",
            document_id, item.source_page,
        )
        return None
    anchor_ok = bool(item.anchor_source_text) and _on_page(
        item.anchor_source_text or "", pages, item.source_page
    )
    row = DocumentDate(
        document_id=document_id,
        date_value=item.date_value,
        date_type=item.date_type,
        source_page=item.source_page,
        source_text=item.source_text,
        confidence=item.confidence,
        extractor_version=LLM_VERSION,
        pass_name="llm",
        is_derived=item.is_derived,
        # An unverified anchor is dropped rather than shown: the UI would
        # otherwise display arithmetic it cannot stand behind. The date itself
        # survives and is flagged as derived with no anchor.
        anchor_date=item.anchor_date if anchor_ok else None,
        anchor_source_text=item.anchor_source_text if anchor_ok else None,
    )
    session.add(row)
    return row


def extract_dates(
    session: Session,
    document: Document,
    pages: list[PageText],
    *,
    client: ModelClient | None,
    settings: Settings | None,
) -> list[DocumentDate]:
    rows: list[DocumentDate] = []

    if not _already_extracted(session, document.id, REGEX_VERSION):
        for candidate in find_dates(pages):
            row = DocumentDate(
                document_id=document.id,
                date_value=candidate.date_value,
                date_type=candidate.date_type,
                source_page=candidate.source_page,
                source_text=candidate.source_text,
                confidence=candidate.confidence,
                extractor_version=REGEX_VERSION,
                pass_name="regex",
            )
            session.add(row)
            rows.append(row)

    # No model configured is not a failure worth logging a traceback for; the
    # regex floor above has already run, which is the point of having it.
    have_model = client is not None and settings is not None
    if have_model and not _already_extracted(session, document.id, LLM_VERSION):
        try:
            found = find_dates_llm(
                pages, find_dates(pages), client=client, settings=settings
            )
        except Exception:  # noqa: BLE001 - the regex floor must survive this
            logger.exception("date llm pass failed document_id=%s", document.id)
            found = []
        for item in found:
            row = _store_llm_date(session, document.id, item, pages)
            if row is not None:
                rows.append(row)

    session.flush()
    logger.info(
        "dates extracted document_id=%s stored=%s", document.id, len(rows)
    )
    return rows


def status_of(session: Session, document_date_id: int) -> str:
    """Unconfirmed until she says otherwise. The latest event wins, so she can
    change her mind and the history of the change is kept."""
    action = session.scalar(
        select(DateEvent.action)
        .where(DateEvent.document_date_id == document_date_id)
        .order_by(DateEvent.id.desc())
        .limit(1)
    )
    return action or "unconfirmed"


def confirm(session: Session, document_date_id: int, *, actor: str = "human"):
    event = DateEvent(document_date_id=document_date_id, action="confirmed",
                      actor=actor)
    session.add(event)
    session.flush()
    return event


def dismiss(session: Session, document_date_id: int, *, actor: str = "human"):
    event = DateEvent(document_date_id=document_date_id, action="dismissed",
                      actor=actor)
    session.add(event)
    session.flush()
    return event
