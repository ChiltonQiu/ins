"""The settings she can change, and the ones she cannot.

The split is the point: a judgment about how the agency works is hers, a
deployment number is the environment's, and a correctness gate is neither.
"""

import pytest

from renewal.settings_store import (
    BY_KEY, DEFINITIONS, Invalid, effective, parse, read, write,
)
from tests.test_dates_llm import _settings


def test_an_unset_setting_falls_through_to_the_environment(session):
    settings = _settings()
    assert effective(session, settings) is settings


def test_a_stored_value_wins_over_the_environment(session):
    write(session, "attention_premium_pct", "25")
    assert effective(session, _settings()).attention_premium_pct == 25.0


def test_the_newest_row_wins(session):
    write(session, "attention_premium_pct", "25")
    write(session, "attention_premium_pct", "5")
    assert effective(session, _settings()).attention_premium_pct == 5.0


def test_the_older_rows_are_kept(session):
    """What she had it set to last month explains an item that fired last
    month."""
    from renewal.models import AgencySetting

    write(session, "attention_premium_pct", "25")
    write(session, "attention_premium_pct", "5")
    assert session.query(AgencySetting).count() == 2


def test_everything_else_is_left_alone(session):
    """replace() must not disturb a field she never touched."""
    settings = _settings()
    write(session, "attention_premium_pct", "25")
    got = effective(session, settings)
    assert got.extraction_model == settings.extraction_model
    assert got.database_url == settings.database_url


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("notify_enabled", "on", True),
        ("notify_enabled", "", False),
        ("notify_to", "her@agency.com", "her@agency.com"),
        ("notify_to", "", ""),
        ("notify_min_interval_minutes", "30", 30),
        ("attention_premium_pct", "7.5", 7.5),
        ("unconfirmed_date_window_days", "30", 30),
        ("session_ttl_hours", "8", 8),
    ],
)
def test_each_kind_parses(session, key, raw, expected):
    assert parse(BY_KEY[key], raw) == expected


@pytest.mark.parametrize(
    "key,raw",
    [
        ("notify_to", "not-an-address"),
        ("notify_min_interval_minutes", "0"),
        ("notify_min_interval_minutes", "9999"),
        ("notify_min_interval_minutes", "soon"),
        ("attention_premium_pct", "-1"),
        ("attention_premium_pct", "101"),
        ("unconfirmed_date_window_days", "0"),
        ("session_ttl_hours", "0"),
    ],
)
def test_a_value_out_of_bounds_is_refused(session, key, raw):
    with pytest.raises(Invalid):
        write(session, key, raw)


def test_the_refusal_says_what_is_wrong(session):
    with pytest.raises(Invalid, match="cannot be above"):
        write(session, "attention_premium_pct", "500")


def test_an_unknown_key_is_refused(session):
    """The form posts keys; a typo must not become a row nothing reads."""
    with pytest.raises(Invalid):
        write(session, "auto_link_threshold", "0.8")


def test_a_corrupt_stored_row_does_not_take_the_page_down(session):
    """A bad row falls through to the environment rather than raising.

    Nothing writes one today — write() parses first — but a hand-edited
    database must not be able to 500 every page in the application.
    """
    from renewal.models import AgencySetting

    session.add(AgencySetting(agency_id=1, key="session_ttl_hours",
                              value="whenever"))
    session.flush()
    assert "session_ttl_hours" not in read(session)
    assert effective(session, _settings()).session_ttl_hours == 12


def test_a_retired_key_is_ignored(session):
    from renewal.models import AgencySetting

    session.add(AgencySetting(agency_id=1, key="something_removed", value="1"))
    session.flush()
    assert read(session) == {}


def test_a_bool_round_trips_through_storage(session):
    write(session, "notify_enabled", "")
    assert effective(session, _settings()).notify_enabled is False
    write(session, "notify_enabled", "on")
    assert effective(session, _settings()).notify_enabled is True


def test_every_definition_names_a_real_settings_field():
    """A key with no field behind it would be stored, read, and silently
    dropped by replace() — or worse, raise on every page."""
    from renewal.config import Settings

    fields = {f.name for f in __import__("dataclasses").fields(Settings)}
    assert {d.key for d in DEFINITIONS} <= fields


def test_the_auto_link_threshold_is_not_a_setting():
    """D8. A near-miss that files itself reports success while attaching one
    client's renewal to another client's policy."""
    assert "auto_link_threshold" not in BY_KEY
    assert not any("auto_link" in d.key for d in DEFINITIONS)
