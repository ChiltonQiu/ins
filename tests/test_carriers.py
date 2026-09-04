from renewal.carriers import (
    add_alias, admitted_status, resolve_carrier, set_admitted,
    unresolved_carrier_names,
)
from renewal.models import Carrier, Client, Policy


def _carrier(session, name="Progressive Casualty Ins Co"):
    carrier = Carrier(display_name=name)
    session.add(carrier)
    session.flush()
    return carrier


def _policy(session, carrier_name, state=None):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name=carrier_name,
                    policy_number="P-1", line_of_business="commercial_auto",
                    state=state)
    session.add(policy)
    session.flush()
    return policy


def test_an_exact_name_resolves(session):
    carrier = _carrier(session)
    assert resolve_carrier(session, "Progressive Casualty Ins Co").id == carrier.id


def test_case_and_spacing_do_not_matter(session):
    carrier = _carrier(session)
    assert resolve_carrier(session, "  progressive  casualty ins co ").id == carrier.id


def test_an_alias_resolves(session):
    carrier = _carrier(session)
    add_alias(session, carrier.id, "Progressive")
    assert resolve_carrier(session, "Progressive").id == carrier.id


def test_an_unknown_name_resolves_to_nothing_rather_than_a_guess(session):
    _carrier(session)
    assert resolve_carrier(session, "Some Other Insurance Company") is None


def test_admitted_status_is_read_for_the_policys_state(session):
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "non_admitted")
    set_admitted(session, carrier.id, "AZ", "admitted")
    assert admitted_status(session, carrier.id, "CA") == "non_admitted"
    assert admitted_status(session, carrier.id, "AZ") == "admitted"


def test_the_latest_setting_for_a_state_wins(session):
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "unknown")
    set_admitted(session, carrier.id, "CA", "admitted")
    assert admitted_status(session, carrier.id, "CA") == "admitted"


def test_a_state_with_no_setting_is_unknown(session):
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "admitted")
    assert admitted_status(session, carrier.id, "NV") == "unknown"


def test_a_policy_with_no_state_is_unknown_not_assumed(session):
    """A carrier admitted in one state says nothing about another."""
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "admitted")
    assert admitted_status(session, carrier.id, None) == "unknown"


def test_unresolved_names_are_listed_for_her_to_alias(session):
    _carrier(session)
    _policy(session, "Some Other Insurance Company")
    assert "Some Other Insurance Company" in unresolved_carrier_names(session)


def test_a_resolved_name_is_not_listed(session):
    carrier = _carrier(session)
    _policy(session, "Progressive")
    add_alias(session, carrier.id, "Progressive")
    assert unresolved_carrier_names(session) == []
