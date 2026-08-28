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

from renewal.models import Coverage, InsuredItem, PolicyTerm

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


def normalize(field_path: str, value: str | None) -> str | None:
    """Canonicalize type only. Never suppresses a difference."""
    if value is None:
        return None
    text = " ".join(value.split())
    if field_path.split(".")[-1] in MONEY_LEAVES:
        try:
            return str(Decimal(re.sub(r"[$,\s]", "", text)).normalize())
        except InvalidOperation:
            return text
    return text


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

    return {path: value for path, value in field_map.items() if value is not None}


def diff_terms(
    session: Session, prior: PolicyTerm, renewal: PolicyTerm
) -> list[RawDifference]:
    prior_map = term_field_map(session, prior)
    renewal_map = term_field_map(session, renewal)
    differences = []
    for path in sorted(set(prior_map) | set(renewal_map)):
        before, after = prior_map.get(path), renewal_map.get(path)
        if normalize(path, before) != normalize(path, after):
            differences.append(RawDifference(path, before, after))
    return differences
