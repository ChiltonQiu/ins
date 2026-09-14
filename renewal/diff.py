"""Diffing two promoted terms.

Every difference is emitted. Nothing is suppressed here — classification labels
rows later, so the audit trail stays complete and it stays visible how often
each noise rule fires. Matching falls out of the field-path keys: two values
compare only when they describe the same thing on the same vehicle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from renewal.extras import DEFAULT_TYPE
from renewal.models import Coverage, InsuredItem, PolicyTerm, PolicyTermExtra

MONEY_LEAVES = ("total_premium", "premium", "deductible_value")


def _date_str(value: date | str | None) -> str | None:
    """`PolicyTerm.effective_date`/`expiration_date` hold a `date` object once
    promoted normally, but an ORM attribute set from a plain string (as in
    some test fixtures) is never coerced without a DB round-trip. Accept
    either shape rather than assuming one."""
    if isinstance(value, date):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class RawDifference:
    field_path: str
    prior_value: str | None
    renewal_value: str | None


@dataclass(frozen=True)
class FieldSet:
    """One term, flattened. The engine takes these rather than terms so that
    it needs no session and the pairwise case is the same call."""

    term_id: int
    values: dict[str, str | None]


@dataclass(frozen=True)
class MatrixRow:
    """comparand_values is positional: index i is the i-th comparand, and None
    means that column does not have the field at all."""

    field_path: str
    baseline_value: str | None
    comparand_values: list[str | None]


def normalize(
    field_path: str, value: str | None, value_type: str | None = None
) -> str | None:
    """Canonicalize type only. Never suppresses a difference.

    value_type is passed for extras, whose vocabulary is open. Core paths
    leave it None and keep guessing from the leaf name, which is correct for a
    closed vocabulary and wrong for an open one — extras.premium would
    otherwise read a policy number as a quantity.
    """
    if value is None:
        return None
    text = " ".join(value.split())
    money = (
        value_type == "money"
        if value_type is not None
        else field_path.split(".")[-1] in MONEY_LEAVES
    )
    if money:
        try:
            return str(Decimal(re.sub(r"[$,\s]", "", text)).normalize())
        except InvalidOperation:
            return text
    return text


def value_type_of(field_path: str, types: dict[str, str] | None) -> str | None:
    """The type to compare a path under, or None to guess from the leaf.

    Only extras carry a type: everything else is a closed vocabulary whose
    leaves are known. An extra with no entry is text, which is why an
    unconfigured installation shows a difference rather than hiding one.
    """
    if not field_path.startswith("extras."):
        return None
    return (types or {}).get(field_path, DEFAULT_TYPE)


def term_field_map(session: Session, term: PolicyTerm) -> dict[str, str | None]:
    """The term, flattened into canonical field paths."""
    field_map: dict[str, str | None] = {
        "policy.carrier_name": term.carrier_name,
        "policy.policy_number": term.policy_number,
        "policy.effective_date": _date_str(term.effective_date),
        "policy.expiration_date": _date_str(term.expiration_date),
        "policy.total_premium": term.total_premium,
    }

    items = {
        item.id: item
        for item in session.query(InsuredItem).filter_by(policy_term_id=term.id)
    }
    for item in items.values():
        key = item.attributes.get("item_key", item.descriptor)
        if item.item_type == "form":
            field_map[f"forms.{key}.edition_date"] = item.attributes.get("edition_date")
            continue
        field_map[f"item.{key}.descriptor"] = item.descriptor
        for name, value in item.attributes.items():
            if name == "item_key":
                continue
            field_map[f"item.{key}.attributes.{name}"] = value

    for coverage in session.query(Coverage).filter_by(policy_term_id=term.id):
        if coverage.insured_item_id is None:
            prefix = f"coverage.{coverage.coverage_code}"
        else:
            item = items[coverage.insured_item_id]
            key = item.attributes.get("item_key", item.descriptor)
            prefix = f"item.{key}.coverage.{coverage.coverage_code}"
        for leaf in ("limit_value", "limit_basis", "deductible_value", "premium"):
            value = getattr(coverage, leaf)
            if value is not None:
                field_map[f"{prefix}.{leaf}"] = value

    # Extras arrive flat and unmarked: the diff, the rules and the screen
    # never learn that a path is carrier-specific.
    for extra in session.query(PolicyTermExtra).filter_by(policy_term_id=term.id):
        field_map[extra.field_path] = extra.value

    return {path: value for path, value in field_map.items() if value is not None}


def diff_field_sets(
    baseline: FieldSet,
    comparands: list[FieldSet],
    types: dict[str, str] | None = None,
) -> list[MatrixRow]:
    """Every path any column mentions, kept when any comparand disagrees with
    the baseline. Nothing is suppressed here — classification labels rows
    later, exactly as in the pairwise case."""
    paths: set[str] = set(baseline.values)
    for comparand in comparands:
        paths |= set(comparand.values)

    rows = []
    for path in sorted(paths):
        before = baseline.values.get(path)
        after = [comparand.values.get(path) for comparand in comparands]
        value_type = value_type_of(path, types)
        canonical = normalize(path, before, value_type)
        if any(normalize(path, value, value_type) != canonical for value in after):
            rows.append(MatrixRow(path, before, after))
    return rows


def diff_terms(
    session: Session, prior: PolicyTerm, renewal: PolicyTerm
) -> list[RawDifference]:
    """The pairwise case, which is one comparand against one baseline."""
    rows = diff_field_sets(
        FieldSet(prior.id, term_field_map(session, prior)),
        [FieldSet(renewal.id, term_field_map(session, renewal))],
    )
    return [
        RawDifference(row.field_path, row.baseline_value, row.comparand_values[0])
        for row in rows
    ]
