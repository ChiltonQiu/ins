"""The front page: what is happening, before anybody clicks anything.

Nothing here writes, and nothing here is stored. Every number is counted from
rows the pipeline already wrote, on every read, for the same reason the inbox
computes its buckets that way: a cached count is wrong the moment somebody
acts on the thing it counted, and a dashboard that lies is worse than one that
is a little slower.

What belongs on this page is a question with one answer: the things that
change what she does next. Documents waiting on her, dates coming at her, and
whether the intake that feeds both is actually running. Everything else —
what a document became, which rule fired, who decided — lives on the screen
that owns it, one click away.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.calendarview.agenda import agenda
from renewal.inbox import (
    DocumentState, bucketed, inbox_rows, stalled_after_from,
)
from renewal.models import (
    Client, Comparison, Document, MailPollState, Policy, PolicyTerm,
)

# Far enough out to catch a renewal that needs work starting now, near enough
# that the list is still a list rather than a year's calendar.
HORIZON_DAYS = 30

# Enough to show the shape of the queue without turning the front page into
# the inbox it links to.
PREVIEW_ROWS = 5


@dataclass(frozen=True)
class Renewal:
    """One comparison the pipeline built, as a line somebody can read."""

    comparison_id: int
    client_name: str
    policy_number: str
    carrier_name: str
    prior_premium: Decimal | None
    renewal_premium: Decimal | None
    built_at: datetime

    @property
    def delta(self) -> Decimal | None:
        if self.prior_premium is None or self.renewal_premium is None:
            return None
        return self.renewal_premium - self.prior_premium

    @property
    def pct(self) -> float | None:
        """None rather than zero when there is nothing to divide by: a
        percentage against a missing or zero prior premium is not a small
        change, it is not a change anybody can state."""
        if self.delta is None or not self.prior_premium:
            return None
        return float(self.delta / self.prior_premium * 100)


@dataclass(frozen=True)
class Intake:
    """Whether documents can still arrive.

    An intake that broke on Thursday and a quiet week look identical from the
    inbox, which is exactly the failure this line exists to make visible.
    """

    configured: bool
    host: str | None = None
    folder: str | None = None
    last_polled_at: datetime | None = None
    last_error: str | None = None
    last_seen: int = 0
    last_ingested: int = 0

    @property
    def healthy(self) -> bool:
        return self.configured and not self.last_error


def _money(raw: str | None) -> Decimal | None:
    """Premiums are stored as the text the document showed. A value that does
    not parse is not zero — it is a value this page has nothing to say about,
    and saying nothing is the only honest option."""
    if not raw:
        return None
    cleaned = raw.replace(",", "").replace("$", "").strip()
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def recent_renewals(session: Session, *, limit: int = PREVIEW_ROWS) -> list[Renewal]:
    """The comparisons that built themselves, newest first.

    This is the only part of the page that reports work already done rather
    than work waiting. It earns the space: it is the evidence that the
    automatic half is running at all, and without it the front page is a list
    of chores.
    """
    prior = PolicyTerm.__table__.alias("prior_term")
    renewal = PolicyTerm.__table__.alias("renewal_term")
    rows = session.execute(
        select(
            Comparison.id, Comparison.created_at, Client.display_name,
            Policy.policy_number, Policy.carrier_name,
            prior.c.total_premium, renewal.c.total_premium,
        )
        .join(renewal, renewal.c.id == Comparison.renewal_term_id)
        .outerjoin(prior, prior.c.id == Comparison.prior_term_id)
        .join(Policy, Policy.id == renewal.c.policy_id)
        .join(Client, Client.id == Policy.client_id)
        .order_by(Comparison.created_at.desc())
        .limit(limit)
    )
    return [
        Renewal(
            comparison_id=cid, client_name=client, policy_number=number,
            carrier_name=carrier, prior_premium=_money(before),
            renewal_premium=_money(after), built_at=created,
        )
        for cid, created, client, number, carrier, before, after in rows
    ]


def intake_state(session: Session, settings) -> Intake:
    if not settings.imap_host:
        return Intake(configured=False)
    row = session.scalar(
        select(MailPollState)
        .where(MailPollState.host == settings.imap_host)
        .where(MailPollState.folder == settings.imap_folder)
    )
    if row is None:
        # Configured but never polled: the clock has not come round yet, or
        # this process has only just started. Not an error, and not health.
        return Intake(configured=True, host=settings.imap_host,
                      folder=settings.imap_folder)
    return Intake(
        configured=True, host=row.host, folder=row.folder,
        last_polled_at=row.last_polled_at, last_error=row.last_error,
        last_seen=row.last_seen, last_ingested=row.last_ingested,
    )


def arrived_since(session: Session, days: int = 7) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return session.scalar(
        select(func.count(Document.id)).where(Document.uploaded_at >= since)
    ) or 0


def overview(session: Session, *, settings, today: date | None = None) -> dict:
    """Everything the front page shows, in one call.

    The buckets come from the inbox's own function rather than a second count
    written here: two implementations of "what needs her" would eventually
    disagree, and the one on the front page would be the one nobody noticed
    was wrong.
    """
    today = today or date.today()
    states = inbox_rows(
        session,
        stalled_after=stalled_after_from(settings),
        limit=settings.inbox_limit,
    )
    groups = bucketed(states)
    dates = [
        entry for entry in agenda(
            session, agency_id=1, start=today,
            end=today + timedelta(days=HORIZON_DAYS),
        )
        if entry.status != "dismissed"
    ]
    dates.sort(key=lambda e: (e.date_value, e.client_name or ""))

    needs_you: list[DocumentState] = groups["needs_you"]
    working: list[DocumentState] = groups["working"]
    return {
        "counts": {
            "needs_you": len(needs_you),
            "working": len(working),
            "stalled": sum(1 for s in working if s.bucket == "stalled"),
            "dates": len(dates),
            "escalated": sum(1 for e in dates if e.escalated),
            "arrived_week": arrived_since(session),
        },
        "needs_you": needs_you[:PREVIEW_ROWS],
        "needs_you_total": len(needs_you),
        "working": working[:PREVIEW_ROWS],
        "dates": dates[:PREVIEW_ROWS + 2],
        "dates_total": len(dates),
        "renewals": recent_renewals(session),
        "intake": intake_state(session, settings),
        "today": today,
    }
