from datetime import date, timedelta

import pytest

from renewal.carriers import set_admitted
from renewal.clients.overview import RENEWAL_WINDOW_DAYS, overview
from renewal.models import (
    Carrier, Client, Document, DocumentDate, DocumentLink, Policy,
    PolicyBillingType, PolicyTerm,
)

TODAY = date(2026, 6, 1)


def _client(session, name="Acme Landscaping LLC"):
    client = Client(display_name=name)
    session.add(client)
    session.flush()
    return client


def _policy(session, client, *, carrier="Travelers", state="CA", expires=None):
    policy = Policy(client_id=client.id, carrier_name=carrier,
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto", state=state)
    session.add(policy)
    session.flush()
    session.add(PolicyTerm(policy_id=policy.id, carrier_name=carrier,
                           effective_date=date(2025, 7, 1),
                           expiration_date=expires or date(2026, 7, 1),
                           total_premium="4820.00"))
    session.flush()
    return policy


def test_policies_are_listed_with_their_term_dates(session):
    client = _client(session)
    _policy(session, client)
    got = overview(session, client.id, agency_id=1, today=TODAY)
    assert got.policies[0].expiration_date == date(2026, 7, 1)
    assert got.policies[0].total_premium == "4820.00"


def test_admitted_status_is_read_for_the_policys_state(session):
    client = _client(session)
    _policy(session, client, carrier="Scottsdale Insurance Company", state="CA")
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    session.add(carrier)
    session.flush()
    set_admitted(session, carrier.id, "CA", "non_admitted")
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].admitted == "non_admitted"


def test_an_unresolvable_carrier_is_unknown_not_assumed(session):
    client = _client(session)
    _policy(session, client, carrier="Some Company We Have Never Seen")
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].admitted == "unknown"


def test_billing_type_defaults_to_unknown(session):
    client = _client(session)
    _policy(session, client)
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].billing_type == "unknown"


def test_her_billing_value_wins_and_a_disagreeing_term_is_flagged(session):
    """A disagreement means billing changed at renewal, which is worth seeing."""
    client = _client(session)
    policy = _policy(session, client)
    session.add(PolicyBillingType(policy_id=policy.id,
                                  billing_type="agency_bill", set_by="human"))
    term = session.query(PolicyTerm).filter_by(policy_id=policy.id).one()
    term.billing_type = "direct_bill"
    session.flush()
    row = overview(session, client.id, agency_id=1, today=TODAY).policies[0]
    assert row.billing_type == "agency_bill"
    assert row.billing_type_from_term == "direct_bill"
    assert row.billing_mismatch is True


def test_agreeing_values_are_not_flagged(session):
    client = _client(session)
    policy = _policy(session, client)
    session.add(PolicyBillingType(policy_id=policy.id,
                                  billing_type="direct_bill", set_by="human"))
    term = session.query(PolicyTerm).filter_by(policy_id=policy.id).one()
    term.billing_type = "direct_bill"
    session.flush()
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].billing_mismatch is False


def test_renewal_countdown_inside_the_window(session):
    client = _client(session)
    _policy(session, client, expires=TODAY + timedelta(days=30))
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].days_to_renewal == 30


def test_no_countdown_outside_the_window(session):
    client = _client(session)
    _policy(session, client, expires=TODAY + timedelta(days=RENEWAL_WINDOW_DAYS + 1))
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].days_to_renewal is None


def test_an_expired_term_shows_a_negative_countdown(session):
    """She needs to see the one she missed, not have it hidden."""
    client = _client(session)
    _policy(session, client, expires=TODAY - timedelta(days=3))
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].days_to_renewal == -3


def test_upcoming_dates_carry_their_confirmation_status(session):
    client = _client(session)
    document = Document(blob_sha256="a" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="auto", confidence=1.0, candidates=[]))
    session.add(DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                             date_type="policy_expiration", source_page=1,
                             source_text="x", confidence=0.5,
                             extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    got = overview(session, client.id, agency_id=1, today=TODAY)
    assert got.upcoming_dates[0].status == "unconfirmed"


def test_documents_are_newest_first(session):
    client = _client(session)
    for name in ("old.pdf", "new.pdf"):
        document = Document(blob_sha256=name.ljust(64, "0")[:64].replace(".", "0"),
                            original_filename=name, page_count=1,
                            has_text_layer=True, doc_type="dec_page",
                            source="bulk_import", agency_id=1)
        session.add(document)
        session.flush()
        session.add(DocumentLink(document_id=document.id, client_id=client.id,
                                 method="auto", confidence=1.0, candidates=[]))
        session.flush()
    names = [d.original_filename
             for d in overview(session, client.id, agency_id=1,
                               today=TODAY).documents]
    assert names == ["new.pdf", "old.pdf"]


def test_a_client_with_nothing_renders_empty_rather_than_erroring(session):
    client = _client(session)
    got = overview(session, client.id, agency_id=1, today=TODAY)
    assert got.policies == []
    assert got.documents == []
    assert got.attention == []


def test_an_unknown_client_raises(session):
    with pytest.raises(LookupError):
        overview(session, 999999, agency_id=1, today=TODAY)


def test_another_clients_documents_are_not_listed(session):
    """The overview is one client's record, not the agency's."""
    mine = _client(session)
    theirs = _client(session, "Other Client LLC")
    document = Document(blob_sha256="c" * 64, original_filename="theirs.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=theirs.id,
                             method="auto", confidence=1.0, candidates=[]))
    session.flush()
    assert overview(session, mine.id, agency_id=1, today=TODAY).documents == []
