"""Whether a summary goes out now.

The four gates, in the order the function applies them: configured, the hour
has come, not already sent today, and either something is waiting or it has
been quiet long enough to be worth saying so.
"""

import dataclasses
from datetime import date, datetime, timezone

from renewal.digest.send import send_due_digest
from renewal.ingest import ingest_pdf
from renewal.models import NotificationSend
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


def _configured(**overrides):
    base = {
        "smtp_host": "smtp.example.com",
        "notify_to": "her@agency.com",
        "notify_from": "app@agency.com",
        "agency_tz": "UTC",
        "digest_hour": 8,
        "digest_quiet_days": 7,
    }
    return dataclasses.replace(_settings(), **(base | overrides))


def _at(hour, day=15):
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)


def _waiting(session, store, n=1):
    for i in range(n):
        ingest_pdf(
            session, store, data=make_text_pdf([[f"unrecognisable {i}"]]),
            original_filename=f"{i}.pdf", source="manual_upload", agency_id=1,
        )
    session.flush()


def _sends(session, at, **overrides):
    sent = []
    result = send_due_digest(
        session, settings=_configured(**overrides), now=at,
        send=lambda subject, body, **k: sent.append((subject, body)),
    )
    return result, sent


def test_before_the_hour_nothing_goes_out(session, store):
    _waiting(session, store, 2)
    result, sent = _sends(session, _at(6))
    assert not result and sent == []


def test_after_the_hour_it_goes_out(session, store):
    _waiting(session, store, 2)
    result, sent = _sends(session, _at(9))
    assert result
    assert "2 documents" in sent[0][0]
    assert session.query(NotificationSend).one().digest_date == date(2026, 9, 15)


def test_a_second_tick_the_same_day_sends_nothing(session, store):
    """The clock asks every five minutes. It must not send every five
    minutes."""
    _waiting(session, store, 1)
    _sends(session, _at(8))
    result, sent = _sends(session, _at(13))
    assert not result and sent == []


def test_the_next_day_sends_again(session, store):
    _waiting(session, store, 1)
    _sends(session, _at(8))
    result, _ = _sends(session, _at(8, day=16))
    assert result


def test_a_process_that_was_down_at_eight_sends_when_it_comes_back(
    session, store
):
    """A late summary is worth more than none, and there is no event to have
    missed: the deadline is still next Tuesday."""
    _waiting(session, store, 1)
    result, _ = _sends(session, _at(23))
    assert result


def test_the_hour_is_local(session, store):
    """Eight in New York is noon UTC. At eleven UTC it is not yet time."""
    _waiting(session, store, 1)
    result, _ = _sends(session, _at(11), agency_tz="America/New_York")
    assert not result
    result, _ = _sends(session, _at(13), agency_tz="America/New_York")
    assert result


def test_the_first_quiet_day_says_so(session, store):
    """A fresh install has never sent anything, so it has been quiet for
    longer than the window by definition. The first summary going out on an
    empty system is how she learns the wiring works at all — an install that
    stayed silent for a week would be indistinguishable from a broken one,
    which is the failure this whole feature exists to remove."""
    result, sent = _sends(session, _at(9))
    assert result
    assert sent[0][0] == "Nothing needs you"


def test_a_quiet_day_after_a_recent_summary_sends_nothing(session, store):
    session.add(NotificationSend(document_count=0,
                                 digest_date=date(2026, 9, 14)))
    session.flush()
    result, sent = _sends(session, _at(9))
    assert not result and sent == []


def test_quiet_for_long_enough_says_so(session, store):
    """A quiet week and a forwarding rule that broke on Thursday look
    identical from where she sits. This is the only thing that can tell her."""
    session.add(NotificationSend(document_count=0,
                                 digest_date=date(2026, 9, 1)))
    session.flush()
    result, sent = _sends(session, _at(9))
    assert result
    assert sent[0][0] == "Nothing needs you"


def test_quiet_but_recently_said_so_stays_quiet(session, store):
    session.add(NotificationSend(document_count=0,
                                 digest_date=date(2026, 9, 13)))
    session.flush()
    result, sent = _sends(session, _at(9))
    assert not result and sent == []


def test_notifications_off_means_no_summary(session, store):
    _waiting(session, store, 1)
    result, _ = _sends(session, _at(9), digest_enabled=False)
    assert not result
    result, _ = _sends(session, _at(9), notify_enabled=False)
    assert not result


def test_no_mail_server_means_no_summary(session, store):
    _waiting(session, store, 1)
    result, _ = _sends(session, _at(9), smtp_host="")
    assert not result
    assert session.query(NotificationSend).count() == 0


def test_a_failed_send_writes_no_row_and_retries(session, store):
    """Nothing may claim a send that did not happen. The next tick tries again
    rather than being locked out by a failure it had no part in."""
    _waiting(session, store, 1)

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    assert not send_due_digest(
        session, settings=_configured(), now=_at(9), send=boom
    )
    assert session.query(NotificationSend).count() == 0

    result, _ = _sends(session, _at(9))
    assert result


def test_a_broken_timezone_still_sends(session, store):
    """Fails toward noise. A typo in AGENCY_TZ sends at the wrong hour; it
    does not send never."""
    _waiting(session, store, 1)
    result, _ = _sends(session, _at(9), agency_tz="Mars/Olympus")
    assert result
