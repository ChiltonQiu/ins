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
