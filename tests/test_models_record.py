from renewal.models import Agency, Carrier, CarrierAdmittedStatus, CarrierAlias


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
