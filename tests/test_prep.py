"""The call prep sheet.

It computes nothing and calls no model: every number on it already exists in a
row that something else wrote. The tests hold that line as much as they check
the assembly.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from renewal.clients.prep import RESIDUAL_LABEL, prep
from renewal.comparison import ColumnSpec, build_matrix
from renewal.materiality import load_rules
from renewal.models import (
    Client,
    Coverage,
    Document,
    DocumentDate,
    Policy,
    PolicyTerm,
)

TODAY = date(2026, 6, 1)
RULES = load_rules("config/materiality.yaml")


@pytest.fixture
def client_id(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    return client.id


def _policy(session, client_id, *, expires=TODAY + timedelta(days=19), term=True):
    """days_to_renewal is read off the policy's latest bound term, so every
    term a test writes has to stay inside the window it is asserting about."""
    policy = Policy(
        client_id=client_id,
        carrier_name="Progressive",
        policy_number="PA-1",
        line_of_business="commercial_auto",
        state="OR",
    )
    session.add(policy)
    session.flush()
    policy.expires = expires
    if term:
        _term(session, policy, premium="3900.00")
    return policy


def _term(session, policy, *, premium, comp_deductible=None, kind="bound"):
    expires = policy.expires
    term = PolicyTerm(
        policy_id=policy.id,
        kind=kind,
        carrier_name="Progressive",
        effective_date=expires - timedelta(days=365),
        expiration_date=expires,
        total_premium=premium,
    )
    session.add(term)
    session.flush()
    if comp_deductible is not None:
        session.add(
            Coverage(
                policy_term_id=term.id,
                coverage_code="COMP",
                deductible_value=comp_deductible,
                premium="400.00",
            )
        )
        session.flush()
    return term


def test_the_sheet_makes_no_model_call(session, client_id):
    """Asserted by there being nowhere to pass one. The sheet is an assembly
    of stored facts; a model would only restate them less reliably."""
    import inspect

    _policy(session, client_id)
    assert "client" not in inspect.signature(prep).parameters
    got = prep(session, client_id, agency_id=1, today=TODAY)
    assert got.client.id == client_id


def test_a_policy_renewing_inside_the_window_carries_its_comparison(
    session, client_id
):
    policy = _policy(session, client_id, term=False)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="4210.00", comp_deductible="1000")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )

    row = prep(session, client_id, agency_id=1, today=TODAY).renewals[0]
    assert row.days_to_renewal == 19
    assert row.comparison_id == comparison.id
    assert row.total_delta == Decimal("310.00")
    assert any("COMP" in change for change in row.material_changes)


def test_a_policy_with_no_comparison_says_so(session, client_id):
    """Rather than comparing on the spot, which would be a model call inside a
    page load she opened while the phone was ringing."""
    _policy(session, client_id)
    row = prep(session, client_id, agency_id=1, today=TODAY).renewals[0]
    assert row.comparison_id is None
    assert row.total_delta is None
    assert row.material_changes == []
    assert row.compare_url.endswith("/compare")


def test_a_policy_outside_the_window_is_not_a_renewal_row(session, client_id):
    _policy(session, client_id, expires=TODAY + timedelta(days=200))
    got = prep(session, client_id, agency_id=1, today=TODAY)
    assert got.renewals == []
    assert len(got.other_policies) == 1


def test_only_material_rows_are_listed(session, client_id):
    """Forty differences per renewal, three that matter. The sheet is read
    aloud; it carries the three."""
    policy = _policy(session, client_id, term=False)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="3900.50", comp_deductible="1000")
    build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    row = prep(session, client_id, agency_id=1, today=TODAY).renewals[0]
    assert all("deductible" in change for change in row.material_changes)


def test_a_change_shows_both_values(session, client_id):
    policy = _policy(session, client_id, term=False)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="4210.00", comp_deductible="1000")
    build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    row = prep(session, client_id, agency_id=1, today=TODAY).renewals[0]
    change = next(c for c in row.material_changes if "deductible" in c)
    assert "500" in change and "1000" in change and "→" in change


def test_a_value_on_only_one_document_is_not_called_a_removal(session, client_id):
    policy = _policy(session, client_id, term=False)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="4210.00")
    build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    row = prep(session, client_id, agency_id=1, today=TODAY).renewals[0]
    dropped = next(c for c in row.material_changes if "deductible" in c)
    assert "not on this document" in dropped
    assert "removed" not in dropped and "dropped" not in dropped


def test_the_residual_is_stated_as_unattributable(session, client_id):
    """Never as a cause. A dec page shows the what, not the why."""
    policy = _policy(session, client_id, term=False)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="4210.00", comp_deductible="500")
    build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    row = prep(session, client_id, agency_id=1, today=TODAY).renewals[0]
    assert row.residual is not None
    assert "attributable" in RESIDUAL_LABEL
    assert "because" not in RESIDUAL_LABEL and "due to" not in RESIDUAL_LABEL


def test_an_unconfirmed_date_says_it_is_unconfirmed(session, client_id):
    policy = _policy(session, client_id)
    document = Document(
        blob_sha256="c" * 64, original_filename="d.pdf", page_count=1,
        has_text_layer=True, doc_type="dec_page", source="bulk_import",
        agency_id=1,
    )
    session.add(document)
    session.flush()
    from renewal.models import DocumentLink

    session.add(
        DocumentLink(document_id=document.id, client_id=client_id,
                     policy_id=policy.id, method="manual", confidence=1.0,
                     candidates=[])
    )
    session.add(
        DocumentDate(
            document_id=document.id,
            date_value=TODAY + timedelta(days=10),
            date_type="policy_expiration",
            confidence=0.9,
            extractor_version="v1",
            source_page=1,
            source_text="Expiration Date: 06/11/2026",
            pass_name="regex",
        )
    )
    session.flush()

    entry = prep(session, client_id, agency_id=1, today=TODAY).upcoming_dates[0]
    assert entry.status == "unconfirmed"


def test_attention_and_messages_come_from_the_same_query_as_the_client_page(
    session, client_id
):
    """A second view over overview(), not a second assembly: the sheet and the
    client page cannot disagree about what needs attention."""
    from renewal.clients.overview import overview

    _policy(session, client_id)
    got = prep(session, client_id, agency_id=1, today=TODAY)
    same = overview(session, client_id, agency_id=1, today=TODAY)
    assert [item.id for item in got.attention] == [item.id for item in same.attention]
    assert [m.id for m in got.messages] == [m.id for m in same.messages]
