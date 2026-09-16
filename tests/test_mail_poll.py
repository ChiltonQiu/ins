"""Pulling mail out of a mailbox.

Nothing here talks to a real server: the poll is handed a fake mailbox, so what
is tested is the sequencing, the dedupe and the failure handling rather than
somebody's TLS stack.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import MailPollState


def test_one_row_per_host_and_folder(session):
    session.add(MailPollState(host="imap.example.com", folder="Carriers"))
    session.flush()
    session.add(MailPollState(host="imap.example.com", folder="Carriers"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_same_folder_on_two_hosts_is_two_rows(session):
    session.add(MailPollState(host="imap.example.com", folder="INBOX"))
    session.add(MailPollState(host="imap.other.com", folder="INBOX"))
    session.flush()
    assert session.query(MailPollState).count() == 2


def test_a_fresh_row_has_seen_nothing(session):
    row = MailPollState(host="imap.example.com", folder="INBOX")
    session.add(row)
    session.flush()
    assert row.last_uid is None
    assert row.uid_validity is None
    assert row.last_error is None


import dataclasses
import email.utils

from renewal.mail.poll import poll_once
from renewal.models import Agency, Document, InboundMessage
from tests.test_dates_llm import StubClient, _settings


def _raw(subject, message_id, body="A cancellation takes effect 2026-12-01."):
    return (
        f"From: underwriting@carrier.example\r\n"
        f"To: anne@agency.example\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Date: {email.utils.formatdate()}\r\n"
        f"\r\n{body}\r\n"
    ).encode()


class FakeBox:
    """Stands in for Mailbox: the same three methods the poll uses."""

    def __init__(self, messages, validity=99):
        self.messages = messages
        self.validity = validity
        self.fetched = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def uid_validity(self):
        return self.validity

    def uids_since(self, uid):
        return sorted(u for u in self.messages if uid is None or u > uid)

    def fetch(self, uid):
        self.fetched.append(uid)
        return self.messages[uid]


def _imap_settings(**overrides):
    base = {"imap_host": "imap.example.com", "imap_user": "anne",
            "imap_password": "app-password", "imap_folder": "Carriers"}
    return dataclasses.replace(_settings(), **(base | overrides))


def _poll(session, store, box, **kwargs):
    return poll_once(
        session, store, settings=_imap_settings(),
        client=StubClient('{"dates": []}'), opener=lambda settings: box,
        **kwargs,
    )


def test_new_mail_becomes_a_message_and_a_document(session, store):
    box = FakeBox({4: _raw("Cancellation", "<a@carrier.example>")})
    result = _poll(session, store, box)

    assert result.seen == 1 and result.ingested == 1
    assert session.query(InboundMessage).count() == 1
    # The body is a document too: the explanation is often in prose while the
    # attachment is a bare form.
    assert session.query(Document).count() == 1


def test_the_same_message_twice_is_ingested_once(session, store):
    box = FakeBox({4: _raw("Cancellation", "<a@carrier.example>")})
    _poll(session, store, box)
    session.query(MailPollState).delete()   # forget the mark, keep the ledger
    session.flush()

    result = _poll(session, store, box)

    assert result.duplicate == 1
    assert result.ingested == 0
    assert session.query(InboundMessage).count() == 1


def test_the_mark_advances_so_the_next_poll_asks_for_less(session, store):
    box = FakeBox({4: _raw("One", "<1@c.example>"),
                   9: _raw("Two", "<2@c.example>")})
    _poll(session, store, box)

    state = session.query(MailPollState).one()
    assert state.last_uid == 9
    assert state.uid_validity == 99

    box.fetched.clear()
    _poll(session, store, box)
    assert box.fetched == []


def test_a_rebuilt_folder_is_read_again_from_the_start(session, store):
    """A changed UIDVALIDITY means every UID remembered is a number about a
    folder that no longer exists."""
    box = FakeBox({4: _raw("One", "<1@c.example>")})
    _poll(session, store, box)

    box.validity = 100
    box.fetched.clear()
    result = _poll(session, store, box)

    assert box.fetched == [4]
    assert result.duplicate == 1      # re-read, and the ledger still holds


def test_one_bad_message_does_not_take_the_rest_of_the_poll_with_it(
    session, store
):
    """The NUL byte is the realistic case rather than a contrived one: the
    email parser accepts almost anything, and Postgres refuses a NUL in a text
    column — so the failure lands mid-transaction, where catching the exception
    is not on its own enough to save the messages behind it."""
    box = FakeBox({4: b"\xff\xfe\x00 not a message at all",
                   5: _raw("Real", "<real@c.example>")})
    result = _poll(session, store, box)

    assert result.failed == 1
    assert result.ingested == 1
    assert session.query(InboundMessage).count() == 1
    # Advanced past the bad one: leaving it behind would jam every later
    # message in the folder behind one that will never parse.
    assert session.query(MailPollState).one().last_uid == 5


def test_a_failed_connection_is_recorded_rather_than_raised(session, store):
    """An intake that has been broken since Thursday must not be invisible."""
    def boom(settings):
        raise OSError("connection refused")

    result = poll_once(session, store, settings=_imap_settings(),
                       client=StubClient('{"dates": []}'), opener=boom)

    assert result.seen == 0
    assert "connection refused" in session.query(MailPollState).one().last_error


def test_a_later_success_clears_the_error(session, store):
    def boom(settings):
        raise OSError("connection refused")

    poll_once(session, store, settings=_imap_settings(),
              client=StubClient('{"dates": []}'), opener=boom)
    _poll(session, store, FakeBox({}))

    assert session.query(MailPollState).one().last_error is None


def test_no_imap_host_polls_nothing(session, store):
    result = poll_once(session, store, settings=_imap_settings(imap_host=""),
                       client=None, opener=lambda settings: FakeBox({}))

    assert result.seen == 0
    assert session.query(MailPollState).count() == 0


def test_the_mailbox_decides_the_agency_not_the_envelope(session, store):
    """Every message here is addressed to anne@agency.example, which matches
    no intake address. Routing by recipient would quarantine all of it."""
    box = FakeBox({4: _raw("Cancellation", "<a@carrier.example>")})
    _poll(session, store, box)

    message = session.query(InboundMessage).one()
    assert message.processing_status == "processed"
    assert message.agency_id == session.query(Agency).one().id
