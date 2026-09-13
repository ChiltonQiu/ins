"""The matrix tables.

Insert-only like the rest of the record: a comparison and its columns and
cells are written once at build and never updated. Reclassifying still logs
against the difference row, which is why difference stays the row table.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import (
    Client,
    Comparison,
    ComparisonColumn,
    Difference,
    DifferenceCell,
    Policy,
    PolicyTerm,
    PolicyTermExtra,
)


def _policy(session):
    client = Client(display_name="Acme Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="PA-1",
        line_of_business="commercial_auto",
    )
    session.add(policy)
    session.flush()
    return policy


def _term(session, policy, **kwargs):
    term = PolicyTerm(policy_id=policy.id, **kwargs)
    session.add(term)
    session.flush()
    return term


def test_a_term_is_bound_unless_it_says_otherwise(session):
    """Every term written before this change was a bound policy term, and the
    default records that rather than assuming it."""
    policy = _policy(session)
    assert _term(session, policy).kind == "bound"
    assert _term(session, policy, kind="quoted").kind == "quoted"


def test_a_term_kind_is_one_of_two_words(session):
    policy = _policy(session)
    with pytest.raises(IntegrityError):
        _term(session, policy, kind="maybe")


def test_a_comparison_needs_no_run_and_no_term_ids(session):
    """A comparison assembled from the record has no upload pair behind it.
    Inventing a RenewalRun for it would make the run table lie."""
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    assert comparison.id
    assert comparison.renewal_run_id is None
    assert comparison.prior_term_id is None


def test_columns_are_positional_and_unique(session):
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    session.add(
        ComparisonColumn(
            comparison_id=comparison.id,
            position=0,
            policy_term_id=_term(session, policy).id,
            role="baseline",
        )
    )
    session.flush()
    session.add(
        ComparisonColumn(
            comparison_id=comparison.id,
            position=0,
            policy_term_id=_term(session, policy).id,
            role="comparand",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_comparison_has_at_most_one_baseline(session):
    """Enforced in the database rather than only in build_matrix. Two
    baselines would mean every cell was measured against something different
    depending on which row the reader looked at first."""
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    for position in (0, 1):
        session.add(
            ComparisonColumn(
                comparison_id=comparison.id,
                position=position,
                policy_term_id=_term(session, policy).id,
                role="baseline",
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_cell_holds_a_null_for_a_field_the_column_lacks(session):
    """NULL means the field is not on that document at all, which is a
    different thing from an empty string and renders differently."""
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    column = ComparisonColumn(
        comparison_id=comparison.id,
        position=0,
        policy_term_id=_term(session, policy).id,
        role="baseline",
    )
    difference = Difference(
        comparison_id=comparison.id,
        field_path="coverage.COMP.premium",
        materiality="material",
        rule_id="coverage_premium_change",
    )
    session.add_all([column, difference])
    session.flush()
    cell = DifferenceCell(
        difference_id=difference.id, comparison_column_id=column.id, value=None
    )
    session.add(cell)
    session.flush()
    assert cell.value is None


def test_one_cell_per_difference_and_column(session):
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    column = ComparisonColumn(
        comparison_id=comparison.id,
        position=0,
        policy_term_id=_term(session, policy).id,
        role="baseline",
    )
    difference = Difference(
        comparison_id=comparison.id,
        field_path="policy.total_premium",
        materiality="material",
        rule_id="premium_total_change",
    )
    session.add_all([column, difference])
    session.flush()
    for value in ("3900.00", "4210.00"):
        session.add(
            DifferenceCell(
                difference_id=difference.id,
                comparison_column_id=column.id,
                value=value,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_difference_row_per_field_path(session):
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    for _ in range(2):
        session.add(
            Difference(
                comparison_id=comparison.id,
                field_path="policy.total_premium",
                materiality="material",
                rule_id="premium_total_change",
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_extra_is_unique_per_term_and_path(session):
    """A term is written once at promotion, so a second value for one path
    would mean promotion ran twice into the same row."""
    policy = _policy(session)
    term = _term(session, policy)
    for _ in range(2):
        session.add(
            PolicyTermExtra(
                policy_term_id=term.id,
                field_path="extras.surcharge_total",
                value="120.00",
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_extra_carries_no_type(session):
    """The type belongs to the key, not the term, and lives in
    config/extras.yaml. A column here would be one fact copied into many rows
    that can disagree with each other and with the config."""
    assert not hasattr(PolicyTermExtra, "value_type")
