"""Session lifecycle.

The table exists so that logout revokes. A signed cookie would be cheaper and
would leave a captured token valid until it expired.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from renewal.auth.sessions import (
    create_session, lookup_session, revoke_session, sweep_expired,
)
from renewal.models import User, UserSession


def _user(session, email="anne@agency.com", active=True):
    user = User(email=email, password_hash="scrypt$x", display_name="Anne",
                is_active=active)
    session.add(user)
    session.flush()
    return user


def test_a_new_session_looks_up_to_its_user(session):
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    assert lookup_session(session, token).id == user.id


def test_the_raw_token_is_not_stored(session):
    """Only its digest is. A database dump must not hand over live cookies."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    row = session.query(UserSession).one()
    assert token not in row.token_sha256
    assert row.token_sha256 == hashlib.sha256(token.encode()).hexdigest()


def test_two_sessions_get_different_tokens(session):
    user = _user(session)
    assert create_session(session, user, ttl_hours=12) != create_session(
        session, user, ttl_hours=12
    )


def test_an_unknown_token_is_none(session):
    assert lookup_session(session, "nonsense") is None


def test_an_expired_session_is_none(session):
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    row = session.query(UserSession).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()
    assert lookup_session(session, token) is None


def test_a_session_belonging_to_a_deactivated_user_is_none(session):
    """Turning an account off must end the sessions it already has, not only
    stop it logging in again."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    user.is_active = False
    session.flush()
    assert lookup_session(session, token) is None


def test_lookup_slides_the_expiry(session):
    """An active session must not expire in the middle of a long review."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    row = session.query(UserSession).one()
    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.flush()
    before = row.expires_at
    lookup_session(session, token)
    assert row.expires_at > before


def test_revoking_makes_the_token_dead(session):
    """Logout deletes the row. Clearing the cookie alone would leave a
    captured token working."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    revoke_session(session, token)
    assert lookup_session(session, token) is None
    assert session.query(UserSession).count() == 0


def test_revoking_an_unknown_token_is_quiet(session):
    revoke_session(session, "nonsense")


def test_the_sweep_removes_only_expired_rows(session):
    user = _user(session)
    live = create_session(session, user, ttl_hours=12)
    dead = create_session(session, user, ttl_hours=12)
    dead_digest = hashlib.sha256(dead.encode()).hexdigest()
    row = session.query(UserSession).filter_by(token_sha256=dead_digest).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()
    assert sweep_expired(session) == 1
    assert lookup_session(session, live).id == user.id
