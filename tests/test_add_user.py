"""Account creation from the command line.

There is no signup route. For an agency of a few people, a script that the
person with shell access runs is the whole provisioning story.
"""

import pytest

from renewal.auth.passwords import verify_password
from renewal.models import User
from scripts.add_user import MIN_LENGTH, upsert_user


def test_a_new_address_creates_an_account(session):
    user, created = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    assert created
    assert verify_password("a-long-enough-password", user.password_hash)


def test_an_existing_address_resets_the_password(session):
    upsert_user(session, "anne@agency.com", "a-long-enough-password", "Anne")
    user, created = upsert_user(
        session, "anne@agency.com", "a-different-password", "Anne"
    )
    assert not created
    assert session.query(User).count() == 1
    assert verify_password("a-different-password", user.password_hash)


def test_a_reset_clears_a_lockout(session):
    """Resetting the password is also how a locked-out person gets back in."""
    user, _ = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    user.failed_count = 7
    user.locked_until = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    session.flush()
    user, _ = upsert_user(
        session, "anne@agency.com", "a-different-password", "Anne"
    )
    assert user.failed_count == 0
    assert user.locked_until is None


def test_a_reset_reactivates_a_deactivated_account(session):
    user, _ = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    user.is_active = False
    session.flush()
    user, _ = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    assert user.is_active


def test_the_address_is_matched_case_insensitively(session):
    upsert_user(session, "anne@agency.com", "a-long-enough-password", "Anne")
    _, created = upsert_user(
        session, "Anne@Agency.com", "a-long-enough-password", "Anne"
    )
    assert not created
    assert session.query(User).count() == 1


def test_a_short_password_is_refused(session):
    with pytest.raises(ValueError, match=str(MIN_LENGTH)):
        upsert_user(session, "anne@agency.com", "short", "Anne")


def test_a_password_of_only_spaces_is_refused(session):
    with pytest.raises(ValueError):
        upsert_user(session, "anne@agency.com", " " * 20, "Anne")
