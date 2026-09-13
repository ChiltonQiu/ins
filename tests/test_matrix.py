"""Building and reading an N-way comparison.

One write path: build_matrix. One read path: matrix_for, which also
synthesizes columns and cells for the comparisons written before the matrix
existed, so nothing downstream has to know which kind it is holding.
"""

from decimal import Decimal

import pytest

from renewal.comparison import (
    MAX_COLUMNS,
    ColumnSpec,
    ColumnsRejected,
    build_matrix,
    matrix_for,
)
from renewal.materiality import load_rules
from renewal.models import Client, Comparison, Coverage, Difference, Policy, PolicyTerm

RULES = load_rules("config/materiality.yaml")


def _client_policy(session, name="Acme Landscaping"):
    client = Client(display_name=name)
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="PA-1",
        line_of_business="commercial_auto",
        state="OR",
    )
    session.add(policy)
    session.flush()
    return policy


def _term(
    session,
    policy,
    *,
    premium,
    carrier="Progressive",
    kind="bound",
    comp_deductible=None,
):
    term = PolicyTerm(
        policy_id=policy.id,
        kind=kind,
        carrier_name=carrier,
        policy_number="PA-1",
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


def test_a_two_column_matrix_carries_the_terms_on_its_columns(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="4210.00")
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(prior.id, "baseline"),
            ColumnSpec(renewal.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert [c.term.id for c in matrix.columns] == [prior.id, renewal.id]
    assert [c.role for c in matrix.columns] == ["baseline", "comparand"]
    assert not matrix.legacy


def test_the_pairwise_columns_are_not_written(session):
    """The same two term ids live on the columns. A second copy on the
    comparison could disagree with them."""
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
            ColumnSpec(_term(session, policy, premium="2").id, "comparand"),
        ],
        rules=RULES,
    )
    assert comparison.prior_term_id is None
    assert comparison.renewal_term_id is None


def test_a_third_column_adds_a_cell_not_a_row(session):
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00")
    b = _term(session, policy, premium="3880.00", carrier="Carrier B", kind="quoted")
    c = _term(session, policy, premium="4455.00", carrier="Carrier C", kind="quoted")
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(baseline.id, "baseline"),
            ColumnSpec(b.id, "comparand"),
            ColumnSpec(c.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    row = next(
        r for r in matrix.rows if r.difference.field_path == "policy.total_premium"
    )
    assert row.baseline.value == "4210.00"
    assert [cell.value for cell in row.comparands] == ["3880.00", "4455.00"]


def test_a_field_a_comparand_lacks_is_a_null_cell(session):
    """Not an empty string. It is usually why the cheaper quote is cheaper."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00", comp_deductible="500")
    quote = _term(
        session, policy, premium="3880.00", carrier="Carrier B", kind="quoted"
    )
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(baseline.id, "baseline"),
            ColumnSpec(quote.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    row = next(
        r
        for r in matrix.rows
        if r.difference.field_path == "coverage.COMP.deductible_value"
    )
    assert row.baseline.value == "500"
    assert row.comparands[0].value is None


def test_the_strongest_comparand_sets_the_row(session):
    """Carrier B matches the incumbent's deductible and Carrier C does not.
    The row is material because under-flagging is the dangerous direction."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00", comp_deductible="500")
    b = _term(
        session,
        policy,
        premium="4210.00",
        kind="quoted",
        carrier="Carrier B",
        comp_deductible="500",
    )
    c = _term(
        session,
        policy,
        premium="4210.00",
        kind="quoted",
        carrier="Carrier C",
        comp_deductible="1000",
    )
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(baseline.id, "baseline"),
            ColumnSpec(b.id, "comparand"),
            ColumnSpec(c.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    row = next(
        r
        for r in matrix.rows
        if r.difference.field_path == "coverage.COMP.deductible_value"
    )
    assert row.difference.materiality == "material"
    assert row.difference.rule_id == "deductible_change"
    assert row.comparands[0].differs is False
    assert row.comparands[1].differs is True


def test_a_matching_comparand_is_not_classified(session):
    """deductible_change matches on path with no `when`, so classifying a pair
    that did not move would return material for a cell that is identical to
    the baseline. Only differing comparands are put to the rules."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00", comp_deductible="500")
    b = _term(
        session,
        policy,
        premium="4210.00",
        kind="quoted",
        carrier="Carrier B",
        comp_deductible="500",
    )
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(baseline.id, "baseline"),
            ColumnSpec(b.id, "comparand"),
        ],
        rules=RULES,
    )
    paths = [r.difference.field_path for r in matrix_for(session, comparison).rows]
    assert "coverage.COMP.deductible_value" not in paths


def test_attribution_runs_between_two_bound_terms(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="4210.00", comp_deductible="1000")
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(prior.id, "baseline"),
            ColumnSpec(renewal.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.columns[1].total_delta == Decimal("310.00")
    assert matrix.columns[1].breakdown.available
    assert matrix.columns[1].breakdown_reason is None


def test_every_column_carries_its_own_total(session):
    """The baseline has no delta but does have a total: the premium_change
    rule needs it as the denominator and the prep sheet prints it as the
    'was' figure."""
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="4210.00")
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(prior.id, "baseline"),
            ColumnSpec(renewal.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.columns[0].total == Decimal("3900.00")
    assert matrix.columns[0].total_delta is None
    assert matrix.columns[1].total == Decimal("4210.00")


def test_attribution_is_skipped_for_a_quoted_column(session):
    """Different carriers are different coverage-code vocabularies, so almost
    the whole delta would land in the residual and the breakdown would look
    like an analysis. The total delta is still arithmetic and still shown."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00")
    quote = _term(
        session, policy, premium="3880.00", carrier="Carrier B", kind="quoted"
    )
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(baseline.id, "baseline"),
            ColumnSpec(quote.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.columns[1].total_delta == Decimal("-330.00")
    assert matrix.columns[1].breakdown is None
    assert "vocabular" in matrix.columns[1].breakdown_reason


def test_two_bound_terms_of_one_policy_are_draft_eligible(session):
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
            ColumnSpec(_term(session, policy, premium="2").id, "comparand"),
        ],
        rules=RULES,
    )
    assert matrix_for(session, comparison).draft_eligible


def test_a_quoted_column_is_not_draft_eligible(session):
    """Any wording that sets carriers side by side is a recommendation."""
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
            ColumnSpec(
                _term(session, policy, premium="2", kind="quoted").id, "comparand"
            ),
        ],
        rules=RULES,
    )
    assert not matrix_for(session, comparison).draft_eligible


def test_three_bound_columns_are_not_draft_eligible(session):
    """A term history has no single 'what changed' to explain."""
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
            ColumnSpec(_term(session, policy, premium="2").id, "comparand"),
            ColumnSpec(_term(session, policy, premium="3").id, "comparand"),
        ],
        rules=RULES,
    )
    assert not matrix_for(session, comparison).draft_eligible


def test_a_comparison_from_before_the_matrix_still_reads(session):
    """No comparison_column rows, so the two term ids and the two value
    columns are what it has. Absence is the only marker."""
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="4210.00")
    comparison = Comparison(prior_term_id=prior.id, renewal_term_id=renewal.id)
    session.add(comparison)
    session.flush()
    session.add(
        Difference(
            comparison_id=comparison.id,
            field_path="policy.total_premium",
            prior_value="3900.00",
            renewal_value="4210.00",
            materiality="material",
            rule_id="premium_total_change",
        )
    )
    session.flush()

    matrix = matrix_for(session, comparison)
    assert matrix.legacy
    assert [c.term.id for c in matrix.columns] == [prior.id, renewal.id]
    assert matrix.rows[0].baseline.value == "3900.00"
    assert matrix.rows[0].comparands[0].value == "4210.00"
    assert matrix.rows[0].comparands[0].differs
    assert matrix.draft_eligible


def test_rows_are_ordered_material_first_then_by_path(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="3901.00", comp_deductible="1000")
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(prior.id, "baseline"),
            ColumnSpec(renewal.id, "comparand"),
        ],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.rows[0].difference.materiality == "material"


def test_noise_can_be_left_out(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="3900.50")
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(prior.id, "baseline"),
            ColumnSpec(renewal.id, "comparand"),
        ],
        rules=RULES,
    )
    assert matrix_for(session, comparison, include_noise=True).rows
    assert not matrix_for(session, comparison, include_noise=False).rows


@pytest.mark.parametrize(
    "reason,build",
    [
        (
            "at most",
            lambda p, t: [ColumnSpec(t(p).id, "baseline")]
            + [ColumnSpec(t(p).id, "comparand") for _ in range(MAX_COLUMNS)],
        ),
        (
            "exactly one baseline",
            lambda p, t: [
                ColumnSpec(t(p).id, "comparand"),
                ColumnSpec(t(p).id, "comparand"),
            ],
        ),
        (
            "first column",
            lambda p, t: [
                ColumnSpec(t(p).id, "comparand"),
                ColumnSpec(t(p).id, "baseline"),
            ],
        ),
    ],
)
def test_the_picker_refusals_are_raised_where_the_rule_lives(session, reason, build):
    policy = _client_policy(session)

    def make(p):
        return _term(session, p, premium="1")

    with pytest.raises(ColumnsRejected, match=reason):
        build_matrix(session, columns=build(policy, make), rules=RULES)


def test_a_quoted_baseline_is_refused(session):
    """Measuring the incumbent against a quote inverts what every other
    column means."""
    policy = _client_policy(session)
    quote = _term(session, policy, premium="1", kind="quoted")
    bound = _term(session, policy, premium="2")
    with pytest.raises(ColumnsRejected, match="bound term"):
        build_matrix(
            session,
            columns=[
                ColumnSpec(quote.id, "baseline"),
                ColumnSpec(bound.id, "comparand"),
            ],
            rules=RULES,
        )


def test_columns_from_two_policies_are_refused(session):
    """The policy chain is what makes these the same risk."""
    first = _client_policy(session)
    second = _client_policy(session, name="Beta Freight")
    with pytest.raises(ColumnsRejected, match="one policy"):
        build_matrix(
            session,
            columns=[
                ColumnSpec(_term(session, first, premium="1").id, "baseline"),
                ColumnSpec(_term(session, second, premium="2").id, "comparand"),
            ],
            rules=RULES,
        )
