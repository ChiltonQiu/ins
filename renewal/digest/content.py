"""What one summary says.

Every number here comes from the function the corresponding page calls:
needs_you_count for the inbox, open_items for the attention queue, agenda for
the calendar. An email that disagrees with the screen she opens from its own
link is worse than no email, because she stops believing both.

Numbers, a date, and a link. Naming a client or a document here would put
client detail into a mailbox, which is exactly what the login exists to
prevent. A bare date carries no such detail and stays, because it is the one
thing this email exists to say.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.attention.rules import open_items
from renewal.calendarview.agenda import agenda
from renewal.config import Settings
from renewal.inbox import needs_you_count
from renewal.models import Document

# There is no tenancy yet; every screen is this one agency's, and so is this.
AGENCY_ID = 1


@dataclass(frozen=True)
class Digest:
    needs_you: int
    attention: int
    upcoming: int
    soonest: date | None
    # Days since anything last arrived, or None on an installation where
    # nothing ever has. This is the number that tells a quiet week apart from
    # a forwarding rule that broke on Thursday.
    quiet_days: int | None

    @property
    def is_quiet(self) -> bool:
        return not (self.needs_you or self.attention or self.upcoming)


def _quiet_days(session: Session, today: date) -> int | None:
    latest = session.scalar(select(func.max(Document.uploaded_at)))
    if latest is None:
        return None
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return (today - latest.astimezone(timezone.utc).date()).days


def collect(
    session: Session, *, settings: Settings, today: date | None = None
) -> Digest:
    """The state of the world, counted the way the pages count it."""
    today = today or date.today()
    window = settings.unconfirmed_date_window_days
    entries = agenda(
        session, agency_id=AGENCY_ID, start=today,
        end=today + timedelta(days=window),
    )
    return Digest(
        needs_you=needs_you_count(session, limit=settings.inbox_limit),
        attention=len(open_items(session, today=today, window_days=window)),
        upcoming=len(entries),
        soonest=min((entry.date_value for entry in entries), default=None),
        quiet_days=_quiet_days(session, today),
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def lines(digest: Digest, *, settings: Settings) -> list[str]:
    """One line per number that is not zero.

    A zero is left out rather than written as a zero: four lines of nothing is
    what an unread email looks like.
    """
    written: list[str] = []
    if digest.needs_you:
        written.append(
            f"{_plural(digest.needs_you, 'document')} in the inbox could not "
            "be filed without a person."
        )
    if digest.attention:
        written.append(
            f"{_plural(digest.attention, 'item')} in the attention queue."
        )
    if digest.upcoming and digest.soonest is not None:
        written.append(
            f"{_plural(digest.upcoming, 'date')} in the next "
            f"{settings.unconfirmed_date_window_days} days — the soonest is "
            f"{digest.soonest.strftime('%a %d %b')}."
        )
    return written


def subject_for(digest: Digest) -> str:
    """Number first, so the count is readable without opening anything."""
    if digest.is_quiet:
        return "Nothing needs you"
    if digest.needs_you:
        verb = "needs" if digest.needs_you == 1 else "need"
        return f"{_plural(digest.needs_you, 'document')} {verb} you"
    if digest.upcoming:
        return f"{_plural(digest.upcoming, 'date')} coming up"
    return f"{_plural(digest.attention, 'item')} in the attention queue"


def render(digest: Digest, *, settings: Settings) -> tuple[str, str]:
    written = lines(digest, settings=settings) or ["Nothing needs you."]
    # The arrival line is the whole point of a quiet summary, and is worth
    # saying in a busy one too: three documents waiting and nothing new in
    # nine days is a forwarding rule that stopped, not a quiet week.
    if (
        digest.quiet_days is not None
        and digest.quiet_days >= settings.digest_quiet_days
    ):
        written.append(f"Nothing has arrived in {digest.quiet_days} days.")
    body = "\n".join(written) + f"\n\n{settings.base_url}/\n"
    return subject_for(digest), body
