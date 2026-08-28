"""Arithmetic premium attribution.

A dec page shows what changed, not why. Every line here is a subtraction of two
numbers printed on the documents; nothing is inferred. Whatever the line items
do not account for is reported as a residual and described as not attributable,
because claiming a rate increase the page does not state is exactly the error
nobody would catch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class Attribution:
    label: str
    field_path: str | None
    amount: Decimal


@dataclass(frozen=True)
class PremiumBreakdown:
    available: bool
    total_delta: Decimal | None
    lines: list[Attribution]
    residual: Decimal | None
    reason: str | None = None


def _money(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(re.sub(r"[$,\s]", "", value)).quantize(TWO_PLACES)
    except InvalidOperation:
        return None


def _label(path: str) -> str:
    parts = path.split(".")
    if parts[0] == "item" and "coverage" in parts:
        return f"{parts[parts.index('coverage') + 1]} on {parts[1]}"
    if parts[0] == "coverage":
        return f"{parts[1]} (policy level)"
    return path


def attribute_premium(
    prior_map: dict[str, str | None], renewal_map: dict[str, str | None]
) -> PremiumBreakdown:
    prior_total = _money(prior_map.get("policy.total_premium"))
    renewal_total = _money(renewal_map.get("policy.total_premium"))
    if prior_total is None or renewal_total is None:
        return PremiumBreakdown(
            available=False,
            total_delta=None,
            lines=[],
            residual=None,
            reason="total premium is not present on both documents",
        )

    total_delta = (renewal_total - prior_total).quantize(TWO_PLACES)
    line_paths = sorted(
        path
        for path in set(prior_map) | set(renewal_map)
        if path.endswith(".premium") and path != "policy.total_premium"
    )

    lines = []
    for path in line_paths:
        before = _money(prior_map.get(path)) or Decimal("0.00")
        after = _money(renewal_map.get(path)) or Decimal("0.00")
        delta = (after - before).quantize(TWO_PLACES)
        if delta:
            lines.append(Attribution(label=_label(path), field_path=path, amount=delta))

    attributed = sum((line.amount for line in lines), Decimal("0.00"))
    return PremiumBreakdown(
        available=True,
        total_delta=total_delta,
        lines=lines,
        residual=(total_delta - attributed).quantize(TWO_PLACES),
    )
