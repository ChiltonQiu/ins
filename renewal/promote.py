"""Promotion freezes one extraction plus its corrections into a policy_term.

The snapshot is written once and never updated. A later correction, or a better
extractor, produces a new term — so every comparison keeps pointing at exactly
the values it was computed from.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from renewal.corrections import effective_values
from renewal.models import (
    Correction,
    Coverage,
    ExtractedField,
    Extraction,
    InsuredItem,
    PolicyTerm,
)

_DATE_PATHS = ("policy.effective_date", "policy.expiration_date")


class PromotionBlocked(Exception):
    """Raised when fields still need review. Promotion is the human gate."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f"unresolved fields: {', '.join(paths)}")


def unresolved_field_paths(
    session: Session, extraction_id: int, acknowledged: frozenset[str] = frozenset()
) -> list[str]:
    corrected_paths = {
        correction.field_path
        for correction in session.query(Correction).filter_by(
            extraction_id=extraction_id
        )
    }
    flagged = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction_id, needs_review=True)
        .order_by(ExtractedField.field_path)
        .all()
    )
    out = []
    for field in flagged:
        if field.field_path in acknowledged:
            continue
        if field.field_path in corrected_paths:
            continue  # a correction was recorded, even if it reverted to the original value
        out.append(field.field_path)
    return out


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _malformed_date_paths(values: dict[str, str | None]) -> list[str]:
    """Date paths that are present and non-empty but do not parse as ISO
    dates. An absent or empty date is legitimate (the carrier didn't print
    it) and is not reported here — only a value that is there but wrong."""
    return [
        path
        for path in _DATE_PATHS
        if values.get(path) and _parse_date(values[path]) is None
    ]


def _assert_stringy(attributes: dict) -> None:
    """attributes is JSONB and would happily hold a native int or bool. Every
    value here comes from effective_values, which yields str | None — assert
    that deliberately rather than by luck, since a JSON number would silently
    break the diff layer's string comparisons."""
    assert all(
        v is None or isinstance(v, str) for v in attributes.values()
    ), "insured_item.attributes must hold strings, not JSON-native types"


def promote(
    session: Session,
    extraction: Extraction,
    policy_id: int,
    *,
    acknowledged: frozenset[str] = frozenset(),
) -> PolicyTerm:
    values = effective_values(session, extraction.id)
    blocked = list(unresolved_field_paths(session, extraction.id, acknowledged))
    for path in _malformed_date_paths(values):
        if path not in blocked:
            blocked.append(path)
    if blocked:
        raise PromotionBlocked(blocked)

    term = PolicyTerm(
        policy_id=policy_id,
        carrier_name=values.get("policy.carrier_name"),
        policy_number=values.get("policy.policy_number"),
        effective_date=_parse_date(values.get("policy.effective_date")),
        expiration_date=_parse_date(values.get("policy.expiration_date")),
        total_premium=values.get("policy.total_premium"),
        source_document_id=extraction.document_id,
        promoted_from_extraction_id=extraction.id,
    )
    session.add(term)
    session.flush()

    # Group the flat field map back into the term's shape.
    policy_coverages: dict[str, dict[str, str | None]] = {}
    items: dict[str, dict] = {}
    forms: dict[str, str | None] = {}

    for path, value in values.items():
        parts = path.split(".")
        if parts[0] == "coverage":
            policy_coverages.setdefault(parts[1], {})[parts[2]] = value
        elif parts[0] == "item":
            item = items.setdefault(
                parts[1], {"descriptor": None, "attributes": {}, "coverages": {}}
            )
            if parts[2] == "descriptor":
                item["descriptor"] = value
            elif parts[2] == "attributes":
                item["attributes"][parts[3]] = value
            elif parts[2] == "coverage":
                item["coverages"].setdefault(parts[3], {})[parts[4]] = value
        elif parts[0] == "forms":
            forms[parts[1]] = value

    for code, leaves in sorted(policy_coverages.items()):
        session.add(
            Coverage(
                policy_term_id=term.id,
                insured_item_id=None,
                coverage_code=code,
                limit_value=leaves.get("limit_value"),
                limit_basis=leaves.get("limit_basis"),
                deductible_value=leaves.get("deductible_value"),
                premium=leaves.get("premium"),
            )
        )

    for key, data in sorted(items.items()):
        attributes = dict(data["attributes"])
        attributes["item_key"] = key
        _assert_stringy(attributes)
        item_row = InsuredItem(
            policy_term_id=term.id,
            item_type="vehicle",
            descriptor=data["descriptor"],
            attributes=attributes,
        )
        session.add(item_row)
        session.flush()
        for code, leaves in sorted(data["coverages"].items()):
            session.add(
                Coverage(
                    policy_term_id=term.id,
                    insured_item_id=item_row.id,
                    coverage_code=code,
                    limit_value=leaves.get("limit_value"),
                    limit_basis=leaves.get("limit_basis"),
                    deductible_value=leaves.get("deductible_value"),
                    premium=leaves.get("premium"),
                )
            )

    for form_number, edition_date in sorted(forms.items()):
        attributes = {"item_key": form_number, "edition_date": edition_date}
        _assert_stringy(attributes)
        session.add(
            InsuredItem(
                policy_term_id=term.id,
                item_type="form",
                descriptor=form_number,
                attributes=attributes,
            )
        )

    session.flush()
    session.refresh(term)
    return term
