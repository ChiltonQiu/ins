"""Materiality classification.

The naive diff produces around forty differences per renewal, of which perhaps
three matter to a client. The filtering is the product, so it lives in a config
file that can be tuned without touching the extractor — not buried in a prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from renewal.diff import RawDifference
from renewal.fieldpath import glob_match

MATERIALITIES = ("material", "informational", "noise")


@dataclass(frozen=True)
class Rule:
    id: str
    materiality: str
    path: str | None = None
    path_glob: str | None = None
    when: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuleSet:
    version: int
    default: str
    rules: list[Rule]


def load_rules(path: Path) -> RuleSet:
    data = yaml.safe_load(Path(path).read_text())
    rules = []
    for entry in data["rules"]:
        if entry["materiality"] not in MATERIALITIES:
            raise ValueError(
                f"rule {entry['id']}: unknown materiality {entry['materiality']}"
            )
        match = entry.get("match", {})
        rules.append(
            Rule(
                id=entry["id"],
                materiality=entry["materiality"],
                path=match.get("path"),
                path_glob=match.get("path_glob"),
                when=entry.get("when", {}),
            )
        )
    return RuleSet(version=data["version"], default=data["default"], rules=rules)


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(re.sub(r"[$,\s]", "", value))
    except InvalidOperation:
        return None


def _conditions_hold(rule: Rule, difference: RawDifference) -> bool:
    """Conditions that need numbers are false when the values are not numbers."""
    if not rule.when:
        return True
    before = _to_decimal(difference.prior_value)
    after = _to_decimal(difference.renewal_value)
    checks = []
    if "abs_delta_gte" in rule.when:
        threshold = Decimal(str(rule.when["abs_delta_gte"]))
        checks.append(
            before is not None and after is not None and abs(after - before) >= threshold
        )
    if "abs_delta_lt" in rule.when:
        threshold = Decimal(str(rule.when["abs_delta_lt"]))
        checks.append(
            before is not None and after is not None and abs(after - before) < threshold
        )
    if "pct_delta_gte" in rule.when:
        threshold = Decimal(str(rule.when["pct_delta_gte"]))
        checks.append(
            before not in (None, Decimal(0))
            and after is not None
            and abs((after - before) / before) >= threshold
        )
    if not checks:
        return True
    return any(checks) if rule.when.get("combine") == "or" else all(checks)


def _matches_path(rule: Rule, path: str) -> bool:
    if rule.path is not None:
        return rule.path == path
    if rule.path_glob is not None:
        return glob_match(rule.path_glob, path)
    return False


def classify(difference: RawDifference, rules: RuleSet) -> tuple[str, str]:
    """Returns (materiality, rule_id). First matching rule wins."""
    for rule in rules.rules:
        if _matches_path(rule, difference.field_path) and _conditions_hold(
            rule, difference
        ):
            return rule.materiality, rule.id
    return rules.default, "default"
