"""Which account made this judgment.

Nullable everywhere and forever: every row written before this change has no
user, and a machine-written row has none by definition. The actor column keeps
saying human-or-machine; user_id says which human.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from renewal.auth.passwords import hash_password
from renewal.ingest import ingest_pdf
from renewal.models import AttentionEvent, AttentionItem, User
from tests.pdfmaker import make_text_pdf

ATTRIBUTED = (
    "correction", "date_event", "manual_date", "manual_date_event",
    "attention_event", "document_link", "comparison", "reclassification",
)


@pytest.fixture
def user(session):
    person = User(email="anne@agency.com", password_hash=hash_password("x"),
                  display_name="Anne Ramirez")
    session.add(person)
    session.flush()
    return person


@pytest.fixture
def item(session, store):
    document = ingest_pdf(
        session, store, data=make_text_pdf([["x"]]),
        original_filename="a.pdf", source="manual_upload", agency_id=1,
    )
    row = AttentionItem(document_id=document.id,
                        reason_code="unmatched_document",
                        reason_text="Could not be attached to a client")
    session.add(row)
    session.flush()
    return row


def test_every_decision_table_has_the_column(session):
    inspector = inspect(session.get_bind())
    for table in ATTRIBUTED:
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        assert "user_id" in columns, f"{table} is not attributed"
        assert columns["user_id"]["nullable"], f"{table}.user_id must be nullable"


def test_a_decision_can_name_its_user(session, user, item):
    event = AttentionEvent(attention_item_id=item.id, action="done",
                           actor="human", user_id=user.id)
    session.add(event)
    session.flush()
    assert session.get(AttentionEvent, event.id).user_id == user.id


def test_a_decision_may_name_nobody(session, item):
    """Three different things mean NULL: written before this change, written
    by the machine, or written by an unauthenticated path. The column does not
    try to tell them apart and neither may anything reading it."""
    event = AttentionEvent(attention_item_id=item.id, action="done",
                           actor="auto")
    session.add(event)
    session.flush()
    assert event.user_id is None


def test_a_user_who_decided_something_cannot_be_deleted(session, user, item):
    """is_active is how an account is turned off. Deleting one would take the
    record of what they decided with it."""
    session.add(AttentionEvent(attention_item_id=item.id, action="done",
                               actor="human", user_id=user.id))
    session.flush()

    with pytest.raises(IntegrityError):
        session.delete(user)
        session.flush()
