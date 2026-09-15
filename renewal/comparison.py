"""Assembling a comparison from N frozen terms.

One baseline column and up to four comparands. A two-term renewal diff is the
two-column case rather than a different object, which is why difference is
still the row table and the cells hang off it.

The comparison, its columns, its rows and its cells are written once.
Reclassifying in the UI logs a reclassification row rather than editing the
difference — that log is the evidence for which rules are wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from renewal.attention.rules import evaluate_comparison
from renewal.carriers import admitted_status, resolve_carrier
from renewal.config import Settings
from renewal.diff import (
    FieldSet,
    MatrixRow,
    RawDifference,
    diff_field_sets,
    normalize,
    term_field_map,
    value_type_of,
)
from renewal.extras import load_types
from renewal.materiality import RuleSet, classify
from renewal.models import (
    Client,
    Comparison,
    ComparisonColumn,
    Difference,
    DifferenceCell,
    Policy,
    PolicyTerm,
    Reclassification,
)
from renewal.premium import PremiumBreakdown, attribute_premium

# A baseline and four comparands. A property of what fits on screen from 390px
# up, not of an installation, so it is a constant rather than a setting — the
# same reasoning that keeps RENEWAL_WINDOW_DAYS out of Settings.
MAX_COLUMNS = 5

_STRENGTH = {"material": 0, "informational": 1, "noise": 2}

_ACROSS_CARRIERS = (
    "line-item attribution is not offered against a quote: a different "
    "carrier is a different coverage-code vocabulary, so almost every line "
    "would fall into the residual"
)


@dataclass(frozen=True)
class ColumnSpec:
    policy_term_id: int
    role: str


class ColumnsRejected(Exception):
    """A refusal with its reason, raised where the rule lives rather than
    re-checked in the route."""


@dataclass(frozen=True)
class Cell:
    value: str | None
    differs: bool


@dataclass(frozen=True)
class Column:
    position: int
    role: str
    term: PolicyTerm
    admitted: str
    total: Decimal | None  # this column's own total premium
    total_delta: Decimal | None  # against the baseline; None on the baseline
    breakdown: PremiumBreakdown | None
    breakdown_reason: str | None


@dataclass(frozen=True)
class Row:
    difference: Difference
    baseline: Cell
    comparands: list[Cell]


@dataclass(frozen=True)
class Matrix:
    comparison: Comparison
    policy: Policy
    client: Client
    columns: list[Column]
    rows: list[Row]
    legacy: bool
    draft_eligible: bool


def _terms_for(session: Session, columns: list[ColumnSpec]) -> list[PolicyTerm]:
    if not columns:
        raise ColumnsRejected("a comparison needs at least one column")
    if len(columns) > MAX_COLUMNS:
        raise ColumnsRejected(
            f"a comparison holds at most {MAX_COLUMNS} columns: a baseline and "
            f"{MAX_COLUMNS - 1} comparands"
        )
    if [spec.role for spec in columns].count("baseline") != 1:
        raise ColumnsRejected("a comparison needs exactly one baseline column")
    if columns[0].role != "baseline":
        raise ColumnsRejected("the baseline is the first column")

    terms = [session.get(PolicyTerm, spec.policy_term_id) for spec in columns]
    missing = [
        spec.policy_term_id for spec, term in zip(columns, terms) if term is None
    ]
    if missing:
        raise ColumnsRejected(f"no such term: {missing}")
    if terms[0].kind != "bound":
        raise ColumnsRejected(
            "the baseline must be a bound term: measuring the incumbent "
            "against a quote inverts what every other column means"
        )
    if len({term.policy_id for term in terms}) != 1:
        raise ColumnsRejected(
            "every column must be a term of one policy: the policy chain is "
            "what makes these the same risk"
        )
    return terms


def _types(settings: Settings | None) -> dict[str, str]:
    """No settings means every extra compares as text, which is the safe
    direction: an unconfigured installation shows a difference rather than
    hiding one."""
    return load_types(settings.extras_config) if settings else {}


def _strongest(
    row: MatrixRow, rules: RuleSet, types: dict[str, str] | None = None
) -> tuple[str, str]:
    """Each comparand that actually differs is classified against the
    baseline, and the strongest answer wins.

    Comparands equal to the baseline are skipped rather than classified. Most
    rules match on path alone — deductible_change has no `when` — so putting
    an unchanged pair to the rules would return material for a cell that did
    not move.

    Strongest wins because under-flagging is the dangerous direction: a row
    where one quote halves the liability limit is material even when every
    other column matches.
    """
    value_type = value_type_of(row.field_path, types)
    canonical = normalize(row.field_path, row.baseline_value, value_type)
    best: tuple[str, str] | None = None
    for value in row.comparand_values:
        if normalize(row.field_path, value, value_type) == canonical:
            continue
        result = classify(
            RawDifference(row.field_path, row.baseline_value, value), rules
        )
        if best is None or _STRENGTH[result[0]] < _STRENGTH[best[0]]:
            best = result
    assert best is not None, "diff_field_sets emits no row where nothing differs"
    return best


def build_matrix(
    session: Session,
    *,
    columns: list[ColumnSpec],
    rules: RuleSet,
    run_id: int | None = None,
    settings: Settings | None = None,
) -> Comparison:
    """The one way a comparison is written.

    settings is threaded from here rather than added later: a later task reads
    extras_config off it and another reads attention_premium_pct, and both are
    then internal changes rather than a signature every caller has to be
    re-edited for. None means neither of those behaviours runs, which is what
    the tests that construct a matrix without settings rely on.
    """
    terms = _terms_for(session, columns)

    comparison = Comparison(renewal_run_id=run_id)
    session.add(comparison)
    session.flush()

    column_rows = []
    for position, spec in enumerate(columns):
        row = ComparisonColumn(
            comparison_id=comparison.id,
            position=position,
            policy_term_id=spec.policy_term_id,
            role=spec.role,
        )
        session.add(row)
        column_rows.append(row)
    session.flush()

    field_sets = [FieldSet(term.id, term_field_map(session, term)) for term in terms]
    baseline_column, *comparand_columns = column_rows
    baseline_set, *comparand_sets = field_sets
    types = _types(settings)

    for row in diff_field_sets(baseline_set, comparand_sets, types):
        materiality, rule_id = _strongest(row, rules, types)
        difference = Difference(
            comparison_id=comparison.id,
            field_path=row.field_path,
            materiality=materiality,
            rule_id=rule_id,
        )
        session.add(difference)
        session.flush()
        session.add(
            DifferenceCell(
                difference_id=difference.id,
                comparison_column_id=baseline_column.id,
                value=row.baseline_value,
            )
        )
        for column, value in zip(comparand_columns, row.comparand_values):
            session.add(
                DifferenceCell(
                    difference_id=difference.id,
                    comparison_column_id=column.id,
                    value=value,
                )
            )

    session.flush()
    session.refresh(comparison)

    # Plain values, never the Matrix: this module imports attention/rules.py,
    # so handing its dataclass across would close an import cycle. Renewals
    # only — across carriers the delta is two carriers pricing the same risk
    # differently, not a change to anything.
    if settings is not None:
        matrix = matrix_for(session, comparison, settings=settings)
        if matrix.draft_eligible:
            evaluate_comparison(
                session,
                document_id=matrix.columns[1].term.source_document_id,
                baseline_total=matrix.columns[0].total,
                total_delta=matrix.columns[1].total_delta,
                settings=settings,
            )
    return comparison


def build_comparison(
    session: Session,
    *,
    prior_term: PolicyTerm,
    renewal_term: PolicyTerm,
    rules: RuleSet,
    run_id: int | None = None,
    settings: Settings | None = None,
) -> Comparison:
    """The pairwise case.

    run_id is optional and is never passed any more: RenewalRun is not written
    by anything after the automatic-intake change. It stays in the signature
    because comparisons built before that change carry one, and it is the only
    record of what those rows meant.
    """
    return build_matrix(
        session,
        columns=[
            ColumnSpec(prior_term.id, "baseline"),
            ColumnSpec(renewal_term.id, "comparand"),
        ],
        rules=rules,
        run_id=run_id,
        settings=settings,
    )


def _total(field_map: dict[str, str | None]) -> Decimal | None:
    """normalize() already canonicalizes total_premium as money, so this needs
    no second money parser and premium.py stays untouched."""
    canonical = normalize("policy.total_premium", field_map.get("policy.total_premium"))
    if canonical is None:
        return None
    try:
        return Decimal(canonical)
    except InvalidOperation:
        return None


def _column(
    session: Session,
    position: int,
    role: str,
    term: PolicyTerm,
    policy: Policy,
    field_map: dict,
    baseline_term: PolicyTerm,
    baseline_map: dict,
) -> Column:
    carrier = resolve_carrier(session, term.carrier_name or "")
    admitted = (
        admitted_status(session, carrier.id, policy.state) if carrier else "unknown"
    )
    total = _total(field_map)

    if role == "baseline":
        # Its own total is carried even though it has no delta: the attention
        # rule needs it as the denominator and the prep sheet prints it as the
        # "was" figure.
        return Column(position, role, term, admitted, total, None, None, None)

    baseline_total = _total(baseline_map)
    delta = (
        (total - baseline_total).quantize(Decimal("0.01"))
        if baseline_total is not None and total is not None
        else None
    )
    # kind, not policy_id: a quote hangs off the incumbent's chain on purpose,
    # so policy_id is equal by construction and cannot discriminate one.
    if "quoted" in (term.kind, baseline_term.kind):
        return Column(
            position, role, term, admitted, total, delta, None, _ACROSS_CARRIERS
        )
    return Column(
        position,
        role,
        term,
        admitted,
        total,
        delta,
        attribute_premium(baseline_map, field_map),
        None,
    )


def matrix_for(
    session: Session,
    comparison: Comparison,
    *,
    include_noise: bool = True,
    settings: Settings | None = None,
) -> Matrix:
    """The one shape every consumer reads.

    A comparison with no comparison_column rows was built before the matrix.
    Its two columns and two cells are synthesized here from the pairwise
    columns, which is the only place in the codebase that knows they exist.
    """
    column_rows = (
        session.query(ComparisonColumn)
        .filter_by(comparison_id=comparison.id)
        .order_by(ComparisonColumn.position)
        .all()
    )
    legacy = not column_rows
    if legacy:
        terms = [
            session.get(PolicyTerm, comparison.prior_term_id),
            session.get(PolicyTerm, comparison.renewal_term_id),
        ]
        roles = ["baseline", "comparand"]
    else:
        terms = [session.get(PolicyTerm, row.policy_term_id) for row in column_rows]
        roles = [row.role for row in column_rows]

    policy = session.get(Policy, terms[0].policy_id)
    client = session.get(Client, policy.client_id)
    field_maps = [term_field_map(session, term) for term in terms]

    columns = [
        _column(
            session, position, role, term, policy, field_map, terms[0], field_maps[0]
        )
        for position, (role, term, field_map) in enumerate(
            zip(roles, terms, field_maps)
        )
    ]

    query = session.query(Difference).filter_by(comparison_id=comparison.id)
    if not include_noise:
        query = query.filter(Difference.materiality != "noise")
    differences = query.all()

    if legacy:
        values = {
            difference.id: (difference.prior_value, [difference.renewal_value])
            for difference in differences
        }
    else:
        by_column = {row.id: index for index, row in enumerate(column_rows)}
        cells = (
            session.query(DifferenceCell)
            .filter(
                DifferenceCell.difference_id.in_(
                    [difference.id for difference in differences]
                )
            )
            .all()
        )
        collected: dict[int, list[str | None]] = {
            difference.id: [None] * len(column_rows) for difference in differences
        }
        for cell in cells:
            collected[cell.difference_id][by_column[cell.comparison_column_id]] = (
                cell.value
            )
        values = {
            difference_id: (row[0], row[1:]) for difference_id, row in collected.items()
        }

    types = _types(settings)
    rows = []
    for difference in differences:
        baseline_value, comparand_values = values[difference.id]
        value_type = value_type_of(difference.field_path, types)
        canonical = normalize(difference.field_path, baseline_value, value_type)
        rows.append(
            Row(
                difference=difference,
                baseline=Cell(baseline_value, False),
                comparands=[
                    Cell(
                        value,
                        normalize(difference.field_path, value, value_type)
                        != canonical,
                    )
                    for value in comparand_values
                ],
            )
        )
    rows.sort(
        key=lambda row: (
            _STRENGTH[row.difference.materiality],
            row.difference.field_path,
        )
    )

    return Matrix(
        comparison=comparison,
        policy=policy,
        client=client,
        columns=columns,
        rows=rows,
        legacy=legacy,
        # Two bound terms of one policy is a renewal, and a renewal is the only
        # thing a client-facing draft can explain without recommending.
        draft_eligible=(
            len(terms) == 2
            and all(term.kind == "bound" for term in terms)
            and terms[0].policy_id == terms[1].policy_id
        ),
    )


def reclassify(
    session: Session,
    difference: Difference,
    to_materiality: str,
    note: str | None = None,
) -> Reclassification:
    log = Reclassification(
        difference_id=difference.id,
        from_materiality=difference.materiality,
        to_materiality=to_materiality,
        rule_id=difference.rule_id,
        note=note,
    )
    session.add(log)
    session.flush()
    return log
