from renewal.models import (
    Agency,
    Carrier,
    CarrierAdmittedStatus,
    CarrierAlias,
    Client,
    Policy,
    PolicyBillingType,
    PolicyTerm,
)


def test_agency_row_exists_with_a_seeded_default(session):
    agency = session.query(Agency).filter_by(slug="default").one()
    assert agency.ics_token
    assert len(agency.ics_token) >= 32


def test_admitted_status_is_keyed_per_state(session):
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    session.add(carrier)
    session.flush()
    session.add_all([
        CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                              status="non_admitted", set_by="human"),
        CarrierAdmittedStatus(carrier_id=carrier.id, state="AZ",
                              status="admitted", set_by="human"),
    ])
    session.flush()
    rows = {r.state: r.status for r in session.query(CarrierAdmittedStatus).all()}
    assert rows == {"CA": "non_admitted", "AZ": "admitted"}


def test_admitted_status_is_append_only_latest_wins(session):
    """Correcting a status inserts; it never updates."""
    carrier = Carrier(display_name="Travelers")
    session.add(carrier)
    session.flush()
    session.add(CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                                      status="unknown", set_by="human"))
    session.flush()
    session.add(CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                                      status="admitted", set_by="human"))
    session.flush()
    rows = session.query(CarrierAdmittedStatus).order_by(
        CarrierAdmittedStatus.id).all()
    assert [r.status for r in rows] == ["unknown", "admitted"]


def test_carrier_alias_resolves_a_name_variant(session):
    carrier = Carrier(display_name="Progressive Casualty Ins Co")
    session.add(carrier)
    session.flush()
    session.add(CarrierAlias(carrier_id=carrier.id, alias="progressive"))
    session.flush()
    found = session.query(CarrierAlias).filter_by(alias="progressive").one()
    assert found.carrier_id == carrier.id


def _policy(session):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name="Travelers",
                    policy_number="P-1", line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    return policy


def test_billing_type_defaults_to_unknown_by_absence(session):
    """No row means unknown. unknown is preferable to a guess."""
    policy = _policy(session)
    assert session.query(PolicyBillingType).filter_by(policy_id=policy.id).count() == 0


def test_billing_type_is_append_only_latest_wins(session):
    policy = _policy(session)
    session.add(PolicyBillingType(policy_id=policy.id, billing_type="unknown",
                                  set_by="human"))
    session.flush()
    session.add(PolicyBillingType(policy_id=policy.id, billing_type="direct_bill",
                                  set_by="human"))
    session.flush()
    rows = session.query(PolicyBillingType).order_by(PolicyBillingType.id).all()
    assert [r.billing_type for r in rows] == ["unknown", "direct_bill"]


def test_policy_term_carries_the_extracted_billing_type(session):
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id, billing_type="agency_bill")
    session.add(term)
    session.flush()
    assert session.get(PolicyTerm, term.id).billing_type == "agency_bill"


def test_policy_term_billing_type_may_be_null(session):
    """Only declarations and endorsements get structured extraction, so most
    terms will never have a value here."""
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id)
    session.add(term)
    session.flush()
    assert session.get(PolicyTerm, term.id).billing_type is None
