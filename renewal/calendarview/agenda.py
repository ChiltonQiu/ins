"""The agenda: every date she needs to see, in one list.

Escalation is deliberately doubled up. An entry is pinned when the document was
classified as a cancellation or non-renewal notice, OR when the date's own type
says so. The second condition means a misclassified notice still escalates —
display never depends on classification being right, which is the whole reason
classification is not allowed to gate anything.

Dates carry no client_id. The client comes from the document's latest link, so
re-filing a misfiled document moves every date on it at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.models import (
    Client, DateEvent, Document, DocumentClassification, DocumentDate,
    DocumentLink, ManualDate, ManualDateEvent,
)

ESCALATED_DATE_TYPES = ("cancellation_effective", "non_renewal_effective")
ESCALATED_DOC_CLASSES = ("cancellation_notice", "non_renewal_notice")
# A dismissed date stays in the database — her judgment is a record, not a
# delete — but it is off the agenda by default, because the agenda is the list
# she works from.
DEFAULT_STATUSES = ("unconfirmed", "confirmed")
ALL_STATUSES = ("unconfirmed", "confirmed", "dismissed")


@dataclass(frozen=True)
class AgendaEntry:
    kind: str
    source_id: int
    date_value: date
    date_type: str
    title: str
    client_id: int | None
    client_name: str | None
    document_id: int | None
    status: str
    confidence: float | None
    is_derived: bool
    anchor_date: date | None
    anchor_source_text: str | None
    source_text: str | None
    source_page: int | None
    escalated: bool


def _latest_link_subquery():
    """One row per document: its most recent link."""
    ranked = select(
        DocumentLink.document_id.label("document_id"),
        DocumentLink.client_id.label("client_id"),
        func.row_number()
        .over(partition_by=DocumentLink.document_id,
              order_by=DocumentLink.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.document_id, ranked.c.client_id).where(
        ranked.c.rn == 1
    ).subquery()


def _latest_class_subquery():
    ranked = select(
        DocumentClassification.document_id.label("document_id"),
        DocumentClassification.doc_class.label("doc_class"),
        func.row_number()
        .over(partition_by=DocumentClassification.document_id,
              order_by=DocumentClassification.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.document_id, ranked.c.doc_class).where(
        ranked.c.rn == 1
    ).subquery()


def _latest_event_subquery(model, fk_name: str):
    column = getattr(model, fk_name)
    ranked = select(
        column.label("parent_id"),
        model.action.label("action"),
        func.row_number()
        .over(partition_by=column, order_by=model.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.parent_id, ranked.c.action).where(
        ranked.c.rn == 1
    ).subquery()


def agenda(
    session: Session,
    *,
    agency_id: int,
    client_id: int | None = None,
    date_types: tuple[str, ...] | None = None,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> list[AgendaEntry]:
    links = _latest_link_subquery()
    classes = _latest_class_subquery()
    events = _latest_event_subquery(DateEvent, "document_date_id")

    query = (
        select(DocumentDate, links.c.client_id, Client.display_name,
               classes.c.doc_class, events.c.action)
        # An extracted date belongs to the agency that holds its document.
        # agency_id is a boundary, not a label on the response.
        .join(Document, Document.id == DocumentDate.document_id)
        .outerjoin(links, links.c.document_id == DocumentDate.document_id)
        .outerjoin(Client, Client.id == links.c.client_id)
        .outerjoin(classes, classes.c.document_id == DocumentDate.document_id)
        .outerjoin(events, events.c.parent_id == DocumentDate.id)
        .where(Document.agency_id == agency_id)
    )
    if client_id is not None:
        query = query.where(links.c.client_id == client_id)
    if date_types:
        query = query.where(DocumentDate.date_type.in_(date_types))
    if start is not None:
        query = query.where(DocumentDate.date_value >= start)
    if end is not None:
        query = query.where(DocumentDate.date_value <= end)

    entries: list[AgendaEntry] = []
    for row, linked_client, client_name, doc_class, action in session.execute(query):
        status = action or "unconfirmed"
        if status not in statuses:
            continue
        entries.append(
            AgendaEntry(
                kind="document_date",
                source_id=row.id,
                date_value=row.date_value,
                date_type=row.date_type,
                title=row.date_type.replace("_", " "),
                client_id=linked_client,
                client_name=client_name,
                document_id=row.document_id,
                status=status,
                confidence=row.confidence,
                is_derived=row.is_derived,
                anchor_date=row.anchor_date,
                anchor_source_text=row.anchor_source_text,
                source_text=row.source_text,
                source_page=row.source_page,
                escalated=(
                    row.date_type in ESCALATED_DATE_TYPES
                    or doc_class in ESCALATED_DOC_CLASSES
                ),
            )
        )

    manual_events = _latest_event_subquery(ManualDateEvent, "manual_date_id")
    manual_query = (
        select(ManualDate, Client.display_name, manual_events.c.action)
        .outerjoin(Client, Client.id == ManualDate.client_id)
        .outerjoin(manual_events, manual_events.c.parent_id == ManualDate.id)
        .where(ManualDate.agency_id == agency_id)
    )
    if client_id is not None:
        manual_query = manual_query.where(ManualDate.client_id == client_id)
    if date_types:
        manual_query = manual_query.where(ManualDate.date_type.in_(date_types))
    if start is not None:
        manual_query = manual_query.where(ManualDate.date_value >= start)
    if end is not None:
        manual_query = manual_query.where(ManualDate.date_value <= end)

    for row, client_name, action in session.execute(manual_query):
        # A date she typed in herself is confirmed by the act of typing it.
        status = action or "confirmed"
        if status not in statuses:
            continue
        entries.append(
            AgendaEntry(
                kind="manual_date",
                source_id=row.id,
                date_value=row.date_value,
                date_type=row.date_type,
                title=row.title,
                client_id=row.client_id,
                client_name=client_name,
                document_id=None,
                status=status,
                confidence=None,
                is_derived=False,
                anchor_date=None,
                anchor_source_text=None,
                source_text=None,
                source_page=None,
                escalated=row.date_type in ESCALATED_DATE_TYPES,
            )
        )

    entries.sort(key=lambda e: (not e.escalated, e.date_value))
    return entries[:limit]
