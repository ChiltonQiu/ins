"""Whether a summary goes out now.

The whole decision in one function, so there is one place to read when the
question is why she did not get an email on Tuesday.

The order of the last two steps is load-bearing. The row is written after the
send and never before: a design that claimed the day first would turn a crash
between claiming and sending into a silently missed day, which is the failure
this whole feature exists to remove. Written afterwards, the worst a race can
do is send two emails — and this application has always preferred a wrong flag
to a missed one.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.digest.content import collect, render
from renewal.models import NotificationSend
from renewal.notify import send_email

logger = logging.getLogger(__name__)


def _zone(settings: Settings) -> ZoneInfo:
    """A typo in AGENCY_TZ costs the right hour, never the summary."""
    try:
        return ZoneInfo(settings.agency_tz)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown AGENCY_TZ %r, using UTC", settings.agency_tz)
        return ZoneInfo("UTC")


def _already_sent(session: Session, today: date) -> bool:
    return session.scalar(
        select(NotificationSend.id)
        .where(NotificationSend.digest_date == today)
        .limit(1)
    ) is not None


def _last_summary(session: Session) -> date | None:
    return session.scalar(select(func.max(NotificationSend.digest_date)))


def send_due_digest(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
    send=None,
) -> bool:
    """Send today's summary if today's summary is due.

    Returns whether one went out. Does not commit — the caller does, exactly
    as maybe_notify leaves the commit to background.py.
    """
    # Both switches: notify_enabled is how she turns this application's mail
    # off altogether, and a summary is not an exception to it.
    if not (settings.digest_enabled and settings.notify_enabled):
        return False
    if not settings.smtp_host or not settings.notify_to:
        return False

    zone = _zone(settings)
    now = now.astimezone(zone) if now else datetime.now(zone)
    today = now.date()

    if now.hour < settings.digest_hour:
        return False
    if _already_sent(session, today):
        return False

    digest = collect(session, settings=settings, today=today)
    if digest.is_quiet:
        last = _last_summary(session)
        if last is not None and (today - last).days < settings.digest_quiet_days:
            return False

    subject, body = render(digest, settings=settings)
    try:
        (send or send_email)(subject, body, settings=settings)
    except Exception:  # noqa: BLE001 - a summary must not cost anything
        logger.exception("digest send failed date=%s", today)
        # No row: nothing was sent, so nothing may claim it was, and the next
        # tick tries again rather than being locked out by a failure it had no
        # part in.
        return False

    session.add(
        NotificationSend(document_count=digest.needs_you, digest_date=today)
    )
    session.flush()
    logger.info("digest sent date=%s needs_you=%s upcoming=%s",
                today, digest.needs_you, digest.upcoming)
    return True
