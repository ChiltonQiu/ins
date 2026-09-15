"""Telling the operator that something is waiting.

The first outbound mail this application sends, and the only one it will send.
The principle it appears to break -- that nothing is sent from here -- is about
client-facing mail and is intact: no draft, no comparison and no client
communication is ever sent automatically. This says a number and a link.

No scheduler here. The background task that produced the backlog is what
notices it — this is the event-triggered half, and it is why
attention/rules.py:13 can go on being true. The clock that speaks in a week
when nothing arrives at all is renewal/digest/clock.py. Both send the same
body, because they are one question asked by two clocks rather than two
notification systems.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.digest.content import collect, lines
from renewal.models import NotificationSend

logger = logging.getLogger(__name__)


def send_email(subject: str, body: str, *, settings: Settings) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.notify_from
    message["To"] = settings.notify_to
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def _last_send(session: Session) -> NotificationSend | None:
    return session.scalar(
        select(NotificationSend).order_by(NotificationSend.id.desc()).limit(1)
    )


def maybe_notify(session: Session, *, settings: Settings, send=None) -> bool:
    """Send one email if anything is waiting and nothing was sent recently.

    Returns whether an email went out. The count is computed the same way the
    page computes it, so the email and the badge cannot disagree.
    """
    # Three ways to be off, and all of them are legitimate: no mail server
    # configured at all, no recipient, or she turned it off from /settings
    # while keeping the address so she can turn it back on.
    if not settings.notify_enabled:
        return False
    if not settings.smtp_host or not settings.notify_to:
        return False

    digest = collect(session, settings=settings)
    count = digest.needs_you
    if count == 0:
        return False

    last = _last_send(session)
    if last is not None:
        sent_at = last.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        window = timedelta(minutes=settings.notify_min_interval_minutes)
        if datetime.now(timezone.utc) - sent_at < window:
            return False

    subject = (
        f"{count} document{'' if count == 1 else 's'} need"
        f"{'s' if count == 1 else ''} you"
    )
    # The same lines the daily summary sends, so the two cannot drift into
    # disagreeing. Still numbers and a link: naming the documents here would
    # put client detail into a mailbox, which is what the login prevents.
    body = (
        "\n".join(lines(digest, settings=settings))
        + f"\n\n{settings.base_url}/\n"
    )

    try:
        (send or send_email)(subject, body, settings=settings)
    except Exception:  # noqa: BLE001 - a notification must not cost a document
        logger.exception("notification send failed count=%s", count)
        # Deliberately no row: nothing was sent, so nothing may claim it was,
        # and the next run tries again instead of being locked out by a
        # failure it had no part in.
        return False

    session.add(NotificationSend(document_count=count))
    session.flush()
    logger.info("notification sent count=%s", count)
    return True
