"""Telling her something is waiting, at most once an hour.

The first outbound mail this application sends. It goes to the operator and
says how many documents need her — never to a client, and never carrying a
draft, a comparison, or anything about a policy.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

from renewal.ingest import ingest_pdf
from renewal.models import NotificationSend
from renewal.notify import maybe_notify
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


def _settings_with_mail(**overrides):
    configured = {
        "smtp_host": "smtp.example.com",
        "notify_to": "her@agency.com",
        "notify_from": "app@agency.com",
    }
    return dataclasses.replace(_settings(), **(configured | overrides))


def _waiting(session, store, n=1):
    for i in range(n):
        ingest_pdf(
            session, store, data=make_text_pdf([[f"unrecognisable {i}"]]),
            original_filename=f"{i}.pdf", source="manual_upload", agency_id=1,
        )
    session.flush()


def _waiting_document(session, store, name="waiting.pdf"):
    document = ingest_pdf(
        session, store, data=make_text_pdf([["unrecognisable"]]),
        original_filename=name, source="manual_upload", agency_id=1,
    )
    session.flush()
    return document


def test_nothing_waiting_sends_nothing(session, store):
    sent = []
    assert not maybe_notify(
        session, settings=_settings_with_mail(),
        send=lambda *a, **k: sent.append(a),
    )
    assert sent == []


def test_something_waiting_sends_once(session, store):
    _waiting(session, store, 2)
    sent = []
    assert maybe_notify(
        session, settings=_settings_with_mail(),
        send=lambda subject, body, **k: sent.append((subject, body)),
    )
    assert len(sent) == 1
    assert "2" in sent[0][0]
    assert session.query(NotificationSend).one().document_count == 2


def test_a_second_call_inside_the_hour_sends_nothing(session, store):
    """A bulk import that strands twenty documents sends one email, not
    twenty."""
    _waiting(session, store, 2)
    sent = []

    def send(subject, body, **kwargs):
        sent.append(subject)

    maybe_notify(session, settings=_settings_with_mail(), send=send)
    _waiting(session, store, 3)
    maybe_notify(session, settings=_settings_with_mail(), send=send)

    assert len(sent) == 1


def test_an_hour_later_it_sends_again(session, store):
    _waiting(session, store, 1)
    sent = []

    def send(subject, body, **kwargs):
        sent.append(subject)

    maybe_notify(session, settings=_settings_with_mail(), send=send)
    row = session.query(NotificationSend).one()
    row.sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.flush()

    maybe_notify(session, settings=_settings_with_mail(), send=send)
    assert len(sent) == 2


def test_no_smtp_host_means_notifications_are_off(session, store):
    _waiting(session, store, 1)
    sent = []
    assert not maybe_notify(
        session, settings=_settings_with_mail(smtp_host=""),
        send=lambda *a, **k: sent.append(a),
    )
    assert sent == []
    assert session.query(NotificationSend).count() == 0


def test_an_smtp_failure_is_swallowed_and_logged(session, store):
    """A notification that cannot be sent must not cost the document."""
    _waiting(session, store, 1)

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    assert not maybe_notify(
        session, settings=_settings_with_mail(), send=boom
    )
    # No row: nothing was sent, so nothing may claim it was. The next call
    # tries again rather than being locked out by a failure it had no part in.
    assert session.query(NotificationSend).count() == 0


def test_the_email_carries_no_client_or_policy_detail(session, store):
    """It says a number and a link. Everything else stays behind the login."""
    _waiting(session, store, 1)
    sent = []
    maybe_notify(
        session, settings=_settings_with_mail(),
        send=lambda subject, body, **k: sent.append(body),
    )
    assert "0.pdf" not in sent[0]
    assert sent[0].count("http") == 1


def test_the_event_email_carries_the_deadline_too(session, store):
    """The same body the daily summary sends. A document arriving is a good
    moment to mention that something else is due on Tuesday, and one body for
    both is what keeps the two from drifting into disagreeing."""
    from datetime import date

    from renewal.models import DocumentDate

    document = _waiting_document(session, store)
    session.add(DocumentDate(
        document_id=document.id, date_value=date.today() + timedelta(days=5),
        date_type="policy_expiration", source_page=1, source_text="Expires",
        confidence=0.9, extractor_version="dates-regex-v1", pass_name="regex",
    ))
    session.flush()

    sent = []
    maybe_notify(
        session, settings=_settings_with_mail(),
        send=lambda subject, body, **k: sent.append(body),
    )
    assert "date" in sent[0]
    assert "soonest" in sent[0]
