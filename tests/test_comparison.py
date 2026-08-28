from pathlib import Path

import pytest

from renewal.comparison import breakdown_for, build_comparison, reclassify
from renewal.materiality import load_rules
from renewal.models import (
    Client,
    Coverage,
    Difference,
    InsuredItem,
    Policy,
    PolicyTerm,
    Reclassification,
    RenewalRun,
    Document,
)


@pytest.fixture(scope="module")
def rules():
    return load_rules(Path("config/materiality.yaml"))


@pytest.fixture
def run_and_terms(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    documents = []
    for suffix in ("1", "2"):
        document = Document(
            blob_sha256=suffix * 64,
            original_filename=f"{suffix}.pdf",
            page_count=1,
            has_text_layer=True,
            doc_type="dec_page",
        )
        session.add(document)
        documents.append(document)
    session.flush()
    run = RenewalRun(
        policy_id=policy.id,
        prior_document_id=documents[0].id,
        renewal_document_id=documents[1].id,
    )
    session.add(run)
    session.flush()

    def term(premium, deductible):
        row = PolicyTerm(
            policy_id=policy.id,
            carrier_name="Progressive",
            policy_number="AU-4471",
            effective_date="2026-03-01",
            total_premium=premium,
        )
        session.add(row)
        session.flush()
        item = InsuredItem(
            policy_term_id=row.id,
            item_type="vehicle",
            descriptor="2018 Ford F-150",
            attributes={"item_key": "VIN0001"},
        )
        session.add(item)
        session.flush()
        session.add(
            Coverage(
                policy_term_id=row.id,
                insured_item_id=item.id,
                coverage_code="COLL",
                deductible_value=deductible,
                premium="412.00" if premium == "1840.00" else "530.00",
            )
        )
        session.flush()
        return row

    return run, term("1840.00", "500"), term("2180.00", "1000")


def test_comparison_records_every_difference_with_its_rule(session, run_and_terms, rules):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    rows = session.query(Difference).filter_by(comparison_id=comparison.id).all()
    by_path = {row.field_path: row for row in rows}
    assert by_path["policy.total_premium"].materiality == "material"
    assert by_path["policy.total_premium"].rule_id == "premium_total_change"
    assert (
        by_path["item.VIN0001.coverage.COLL.deductible_value"].rule_id
        == "deductible_change"
    )
    assert all(row.rule_id for row in rows)


def test_comparison_is_frozen_to_the_terms_it_was_built_from(
    session, run_and_terms, rules
):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    assert comparison.prior_term_id == prior.id
    assert comparison.renewal_term_id == renewal.id
    assert comparison.renewal_run_id == run.id


def test_second_comparison_under_the_same_run_keeps_the_first(
    session, run_and_terms, rules
):
    run, prior, renewal = run_and_terms
    first = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    second = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    assert first.id != second.id
    assert session.query(Difference).filter_by(comparison_id=first.id).count() > 0


def test_breakdown_for_reads_the_frozen_terms(session, run_and_terms, rules):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    breakdown = breakdown_for(session, comparison)
    assert breakdown.available is True
    assert str(breakdown.total_delta) == "340.00"
    assert str(breakdown.residual) == "222.00"


def test_reclassifying_logs_the_change_and_leaves_the_difference(
    session, run_and_terms, rules
):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    difference = (
        session.query(Difference)
        .filter_by(comparison_id=comparison.id, field_path="policy.total_premium")
        .one()
    )
    reclassify(session, difference, "informational", note="client already knew")
    log = session.query(Reclassification).one()
    assert log.from_materiality == "material"
    assert log.to_materiality == "informational"
    assert log.rule_id == "premium_total_change"
    session.refresh(difference)
    assert difference.materiality == "material"  # the difference row is frozen
