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
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.classify.runner import latest_class
from renewal.models import (
    AttentionEvent, AttentionItem, Client, DateEvent, Document, DocumentDate,
    DocumentLink, PolicyTerm,
)
from renewal.resolve.service import latest_link

# The default, not the value. Operator-editable at /settings, because how far
# ahead she wants to be warned is a judgment about how she works.
UNCONFIRMED_DATE_WINDOW_DAYS = 14

# renewal_received and premium_change are written by evaluate_promotion and
# evaluate_comparison rather than by evaluate(), because neither is a fact
# about a document at ingest. The question "is this dec page a renewal or a
# new-business bind?" needed domain knowledge nobody had; the question "is
# this term a renewal of that one?" is arithmetic over two rows.
REASONS = (
    "cancellation_notice",
    "non_renewal_notice",
    "renewal_received",
    "premium_change",
    "unmatched_document",
    "unconfirmed_date_soon",
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
    # Set only on renewal_received: the one click D10 describes. Nothing
    # builds until she takes it.
    compare_url: str | None = None


def _compare_url(session: Session, document_id: int) -> str | None:
    """The picker, with the renewal and the term before it already ticked.

    Built from the two rows the rule itself matched on, so the link cannot
    point at a pair that would not have raised the item.
    """
    term = session.scalar(
        select(PolicyTerm)
        .where(PolicyTerm.source_document_id == document_id)
        .where(PolicyTerm.kind == "bound")
        .order_by(PolicyTerm.id.desc())
        .limit(1)
    )
    if term is None or term.effective_date is None:
        return None
    prior = session.scalar(
        select(PolicyTerm)
        .where(PolicyTerm.policy_id == term.policy_id)
        .where(PolicyTerm.kind == "bound")
        .where(PolicyTerm.id != term.id)
        .where(PolicyTerm.effective_date < term.effective_date)
        .order_by(PolicyTerm.effective_date.desc())
        .limit(1)
    )
    if prior is None:
        return None
    return (
        f"/policies/{term.policy_id}/compare"
        f"?baseline={prior.id}&comparand={term.id}"
    )


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


def open_items(
    session: Session,
    *,
    today: date | None = None,
    window_days: int = UNCONFIRMED_DATE_WINDOW_DAYS,
) -> list[QueueRow]:
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
            compare_url=(
                _compare_url(session, item.document_id)
                if item.reason_code == "renewal_received" else None
            ),
        ))

    judged = select(DateEvent.document_date_id).distinct()
    horizon = today + timedelta(days=window_days)
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
            # Never written to a row — this reason is computed on every read
            # — so the code could be renamed when the window stopped being
            # fixed at fourteen days and the old name became a lie.
            reason_code="unconfirmed_date_soon",
            reason_text=f"Unconfirmed {row.date_type.replace('_', ' ')}",
            due_date=row.date_value, materialised=False,
        ))
    return rows


def resolve(
    session: Session, item_id: int, *, action: str, actor: str = "human",
    user_id: int | None = None,
) -> AttentionEvent:
    event = AttentionEvent(attention_item_id=item_id, action=action,
                           actor=actor, user_id=user_id)
    session.add(event)
    session.flush()
    return event


def evaluate_promotion(session: Session, term: PolicyTerm) -> AttentionItem | None:
    """A bound term on a policy that already holds a bound term with an
    earlier effective date.

    Nothing auto-builds the comparison. The item carries a link to the picker
    with both terms preselected and she clicks it.
    """
    if term.kind != "bound" or term.effective_date is None:
        return None
    earlier = session.scalar(
        select(PolicyTerm.id)
        .where(PolicyTerm.policy_id == term.policy_id)
        .where(PolicyTerm.kind == "bound")
        .where(PolicyTerm.id != term.id)
        .where(PolicyTerm.effective_date < term.effective_date)
        .limit(1)
    )
    if earlier is None or term.source_document_id is None:
        return None
    return _add(
        session, term.source_document_id, "renewal_received",
        "Renewal received. Compare it with the prior term?",
    )


def evaluate_comparison(
    session: Session, *, document_id: int | None, baseline_total: Decimal | None,
    total_delta: Decimal | None, settings,
) -> AttentionItem | None:
    """Renewal comparisons only. Across carriers the delta is two carriers
    pricing the same risk differently, not a change to anything.

    Plain values rather than a Matrix on purpose. This module is imported by
    renewal/comparison.py, so taking its dataclass — or calling matrix_for to
    get one — would close an import cycle. The caller already holds every
    number this needs.
    """
    if document_id is None or total_delta is None or not baseline_total:
        return None
    pct = abs(total_delta / baseline_total) * 100
    if pct < settings.attention_premium_pct:
        return None
    return _add(
        session, document_id, "premium_change",
        f"Premium moved {total_delta:+} ({pct:.0f}%) at renewal",
    )
