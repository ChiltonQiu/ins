"""The two credential tables.

Unlike the record tables these are state, not history: a session row is
deleted at logout and a lockout counter is updated in place.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import User, UserSession


def _user(session, email="anne@agency.com"):
    user = User(email=email, password_hash="scrypt$x", display_name="Anne")
    session.add(user)
    session.flush()
    return user


def test_a_user_defaults_to_active_and_unlocked(session):
    user = _user(session)
    assert user.is_active is True
    assert user.failed_count == 0
    assert user.locked_until is None


def test_two_users_cannot_share_an_address_in_different_cases(session):
    """Uniqueness is on lower(email). Anne@ and anne@ are one person, and two
    rows would mean a login that succeeds or fails depending on shift key."""
    _user(session, "anne@agency.com")
    # Added without flushing, so the IntegrityError is raised by this flush
    # rather than inside the helper, outside the assertion.
    session.add(User(email="Anne@Agency.com", password_hash="scrypt$x",
                     display_name="Anne"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_session_hangs_off_a_user(session):
    user = _user(session)
    now = datetime.now(timezone.utc)
    session.add(UserSession(
        token_sha256="a" * 64, user_id=user.id,
        expires_at=now + timedelta(hours=12), last_seen_at=now,
    ))
    session.flush()
    assert session.query(UserSession).one().user_id == user.id


def test_two_sessions_cannot_share_a_token(session):
    user = _user(session)
    now = datetime.now(timezone.utc)
    for _ in range(2):
        session.add(UserSession(
            token_sha256="a" * 64, user_id=user.id,
            expires_at=now + timedelta(hours=12), last_seen_at=now,
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_a_user_takes_their_sessions_with_them(session):
    user = _user(session)
    now = datetime.now(timezone.utc)
    session.add(UserSession(
        token_sha256="b" * 64, user_id=user.id,
        expires_at=now + timedelta(hours=12), last_seen_at=now,
    ))
    session.flush()
    session.delete(user)
    session.flush()
    assert session.query(UserSession).count() == 0
