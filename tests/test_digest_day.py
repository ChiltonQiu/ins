"""One summary per local day, and the database is what remembers.

A row with digest_date set is a daily summary. A row with it NULL is the
event-triggered email, and there may be any number of those.
"""

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import NotificationSend


def test_two_summaries_for_one_day_cannot_both_exist(session):
    session.add(NotificationSend(document_count=1, digest_date=date(2026, 9, 15)))
    session.flush()
    session.add(NotificationSend(document_count=9, digest_date=date(2026, 9, 15)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_summaries_on_different_days_coexist(session):
    session.add(NotificationSend(document_count=1, digest_date=date(2026, 9, 15)))
    session.add(NotificationSend(document_count=1, digest_date=date(2026, 9, 16)))
    session.flush()
    assert session.query(NotificationSend).count() == 2


def test_the_event_email_is_unconstrained(session):
    """Its digest_date is NULL, and NULLs are distinct. A busy morning may
    send several without either of them being a summary."""
    for _ in range(3):
        session.add(NotificationSend(document_count=1))
    session.flush()
    assert session.query(NotificationSend).count() == 3
