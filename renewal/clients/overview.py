"""Everything about one client, assembled for one screen.

Built for reading while she is on the phone: dense, complete, and never hiding
a basic fact behind a click. Every value it shows is either a stored fact or an
explicit 'unknown' — nothing here infers, and nothing here guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.calendarview.agenda import AgendaEntry, agenda
from renewal.carriers import admitted_status, resolve_carrier
from renewal.models import (
    AttentionEvent, AttentionItem, Client, Document, DocumentClassification,
    DocumentLink, InboundMessage, Policy, PolicyBillingType, PolicyTerm,
)

RENEWAL_WINDOW_DAYS = 60
DOCUMENT_LIMIT = 200
MESSAGE_LIMIT = 10


@dataclass(frozen=True)
class PolicyRow:
    policy_id: int
    carrier_name: str
    policy_number: str
    line_of_business: str
    state: str | None
    effective_date: date | None
    expiration_date: date | None
    total_premium: str | None
    admitted: str
    billing_type: str
    billing_type_from_term: str | None
    billing_mismatch: bool
    days_to_renewal: int | None


@dataclass(frozen=True)
class DocumentRow:
    document_id: int
    original_filename: str
    uploaded_at: datetime
    doc_class: str | None
    source: str


@dataclass(frozen=True)
class ClientOverview:
    client: Client
    policies: list[PolicyRow]
    upcoming_dates: list[AgendaEntry]
    documents: list[DocumentRow]
    messages: list[InboundMessage]
    attention: list[AttentionItem]


def _latest_term(session: Session, policy_id: int) -> PolicyTerm | None:
    return session.scalar(
        select(PolicyTerm)
        .where(PolicyTerm.policy_id == policy_id)
        .order_by(PolicyTerm.id.desc())
        .limit(1)
    )


def _billing_type(session: Session, policy_id: int) -> str:
    return session.scalar(
        select(PolicyBillingType.billing_type)
        .where(PolicyBillingType.policy_id == policy_id)
        .order_by(PolicyBillingType.id.desc())
        .limit(1)
    ) or "unknown"


def _countdown(expiration: date | None, today: date) -> int | None:
    """None outside the window. An expired term returns a negative number
    rather than None: the renewal she missed is the one she most needs to see."""
    if expiration is None:
        return None
    days = (expiration - today).days
    return days if days <= RENEWAL_WINDOW_DAYS else None


def overview(
    session: Session, client_id: int, *, agency_id: int, today: date | None = None
) -> ClientOverview:
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError(f"no client with id {client_id}")
    today = today or date.today()

    policies: list[PolicyRow] = []
    for policy in session.scalars(
        select(Policy).where(Policy.client_id == client_id).order_by(Policy.id)
    ):
        term = _latest_term(session, policy.id)
        carrier = resolve_carrier(session, policy.carrier_name)
        hers = _billing_type(session, policy.id)
        from_term = term.billing_type if term else None
        policies.append(
            PolicyRow(
                policy_id=policy.id,
                carrier_name=policy.carrier_name,
                policy_number=policy.policy_number,
                line_of_business=policy.line_of_business,
                state=policy.state,
                effective_date=term.effective_date if term else None,
                expiration_date=term.expiration_date if term else None,
                total_premium=term.total_premium if term else None,
                admitted=(
                    admitted_status(session, carrier.id, policy.state)
                    if carrier else "unknown"
                ),
                billing_type=hers,
                billing_type_from_term=from_term,
                # Her value wins; a disagreeing term is surfaced rather than
                # overwritten, because it usually means billing changed at
                # renewal and that is worth her seeing.
                billing_mismatch=bool(
                    from_term and hers != "unknown" and from_term != hers
                ),
                days_to_renewal=_countdown(
                    term.expiration_date if term else None, today
                ),
            )
        )

    links = select(DocumentLink.document_id).where(
        DocumentLink.client_id == client_id
    )
    classes = select(
        DocumentClassification.document_id, DocumentClassification.doc_class
    ).subquery()
    documents = [
        DocumentRow(document_id=row[0], original_filename=row[1],
                    uploaded_at=row[2], doc_class=row[3], source=row[4])
        for row in session.execute(
            select(Document.id, Document.original_filename, Document.uploaded_at,
                   classes.c.doc_class, Document.source)
            .outerjoin(classes, classes.c.document_id == Document.id)
            .where(Document.id.in_(links))
            .order_by(Document.uploaded_at.desc(), Document.id.desc())
            .limit(DOCUMENT_LIMIT)
        )
    ]

    resolved = select(AttentionEvent.attention_item_id).distinct()
    attention = list(
        session.scalars(
            select(AttentionItem)
            .where(AttentionItem.document_id.in_(links))
            .where(AttentionItem.id.not_in(resolved))
            .order_by(AttentionItem.id.desc())
        )
    )

    messages = list(
        session.scalars(
            select(InboundMessage)
            .where(InboundMessage.id.in_(
                select(Document.inbound_message_id).where(Document.id.in_(links))
            ))
            .order_by(InboundMessage.received_at.desc())
            .limit(MESSAGE_LIMIT)
        )
    )

    return ClientOverview(
        client=client,
        policies=policies,
        upcoming_dates=agenda(session, agency_id=agency_id, client_id=client_id,
                              start=today),
        documents=documents,
        messages=messages,
        attention=attention,
    )
