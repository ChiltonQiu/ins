"""What appears to need a human response.

Deliberately not a task manager. Every item is a document plus a suggested
reason, and every item is cleared by hand. Nothing here auto-resolves and
nothing here acts: detecting that she replied would require reading sent mail,
which requires OAuth this project does not do.

Rules bias toward over-flagging. A wrong flag costs one keystroke to dismiss; a
missed cancellation notice is the risk this whole system exists to reduce.

Event-triggered reasons are rows, written once at ingest. The one time-based
reason — an unconfirmed date coming up soon — is computed at read time instead,
because it changes with the clock and materialising it would need a scheduler
this design does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.classify.runner import latest_class
from renewal.models import (
    AttentionEvent, AttentionItem, Client, DateEvent, Document, DocumentDate,
    DocumentLink,
)
from renewal.resolve.service import latest_link

UNCONFIRMED_DATE_WINDOW_DAYS = 14

# renewal_received and premium_change are named but not implemented.
# renewal_received needs a definition of what separates a renewal dec page from
# a new-business bind, which is domain knowledge nobody has supplied — a guess
# would be a rule that is confidently wrong. premium_change fires when a
# comparison is built, which belongs to its own plan. Both are declared now so
# the reason vocabulary is stable.
REASONS = (
    "cancellation_notice",
    "non_renewal_notice",
    "renewal_received",
    "premium_change",
    "unmatched_document",
    "unconfirmed_date_within_14_days",
)

_CLASS_REASONS = {
    "cancellation_notice": "Classified as a cancellation notice",
    "non_renewal_notice": "Classified as a non-renewal notice",
}


@dataclass(frozen=True)
class QueueRow:
    item_id: int | None
    document_id: int
    client_id: int | None
    client_name: str | None
    reason_code: str
    reason_text: str
    due_date: date | None
    materialised: bool


def _has_item(session: Session, document_id: int, reason_code: str) -> bool:
    return session.scalar(
        select(AttentionItem.id)
        .where(AttentionItem.document_id == document_id)
        .where(AttentionItem.reason_code == reason_code)
        .limit(1)
    ) is not None


def _add(
    session: Session, document_id: int, reason_code: str, reason_text: str,
    due_date: date | None = None,
) -> AttentionItem | None:
    if _has_item(session, document_id, reason_code):
        return None
    item = AttentionItem(document_id=document_id, reason_code=reason_code,
                         reason_text=reason_text, due_date=due_date)
    session.add(item)
    session.flush()
    return item


def evaluate(session: Session, document: Document, *, settings) -> list[AttentionItem]:
    """Event-triggered reasons only. Idempotent: re-running never duplicates an
    item, and never revives one she has resolved — the existence check looks at
    every item for the document, resolved or not, so her judgment outranks the
    rule that created it."""
    created: list[AttentionItem] = []
    doc_class = latest_class(session, document.id)

    if doc_class in _CLASS_REASONS:
        item = _add(session, document.id, doc_class, _CLASS_REASONS[doc_class])
        if item:
            created.append(item)

    if latest_link(session, document.id) is None:
        item = _add(session, document.id, "unmatched_document",
                    "Could not be attached to a client")
        if item:
            created.append(item)

    return created


def open_items(session: Session, *, today: date | None = None) -> list[QueueRow]:
    today = today or date.today()
    resolved = select(AttentionEvent.attention_item_id).distinct()

    rows: list[QueueRow] = []
    query = (
        select(AttentionItem, DocumentLink.client_id, Client.display_name)
        .outerjoin(DocumentLink,
                   DocumentLink.document_id == AttentionItem.document_id)
        .outerjoin(Client, Client.id == DocumentLink.client_id)
        .where(AttentionItem.id.not_in(resolved))
        .order_by(AttentionItem.id.desc())
    )
    seen: set[int] = set()
    for item, client_id, client_name in session.execute(query):
        if item.id in seen:
            continue
        seen.add(item.id)
        rows.append(QueueRow(
            item_id=item.id, document_id=item.document_id, client_id=client_id,
            client_name=client_name, reason_code=item.reason_code,
            reason_text=item.reason_text, due_date=item.due_date,
            materialised=True,
        ))

    judged = select(DateEvent.document_date_id).distinct()
    horizon = today + timedelta(days=UNCONFIRMED_DATE_WINDOW_DAYS)
    near = (
        select(DocumentDate, DocumentLink.client_id, Client.display_name)
        .outerjoin(DocumentLink,
                   DocumentLink.document_id == DocumentDate.document_id)
        .outerjoin(Client, Client.id == DocumentLink.client_id)
        .where(DocumentDate.id.not_in(judged))
        .where(DocumentDate.date_value >= today)
        .where(DocumentDate.date_value <= horizon)
        .order_by(DocumentDate.date_value)
    )
    for row, client_id, client_name in session.execute(near):
        rows.append(QueueRow(
            item_id=None, document_id=row.document_id, client_id=client_id,
            client_name=client_name,
            reason_code="unconfirmed_date_within_14_days",
            reason_text=f"Unconfirmed {row.date_type.replace('_', ' ')}",
            due_date=row.date_value, materialised=False,
        ))
    return rows


def resolve(
    session: Session, item_id: int, *, action: str, actor: str = "human"
) -> AttentionEvent:
    event = AttentionEvent(attention_item_id=item_id, action=action, actor=actor)
    session.add(event)
    session.flush()
    return event
