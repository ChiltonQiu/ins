"""Scoring for the eval harness. Pure functions, no API calls, so the harness
itself is tested in the default run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    carrier: str
    pdf_filename: str
    fields: dict[str, str]


def load_fixtures(directory: Path) -> list[Fixture]:
    fixtures = []
    for path in sorted(Path(directory).glob("*.json")):
        data = json.loads(path.read_text())
        fixtures.append(
            Fixture(
                fixture_id=data["fixture_id"],
                carrier=data["carrier"],
                pdf_filename=data["pdf_filename"],
                fields=data["fields"],
            )
        )
    return fixtures


def score(expected: dict[str, str], actual: dict[str, str | None]) -> dict[str, bool]:
    """One boolean per labelled field. Fields the fixture does not label are
    ignored: the fixture is the ground truth about what should be found."""
    return {path: actual.get(path) == value for path, value in expected.items()}


def accuracy(results: dict[str, bool]) -> float:
    if not results:
        return 0.0
    return sum(results.values()) / len(results)


def regressions(
    baseline: dict[str, dict[str, bool]], current: dict[str, dict[str, bool]]
) -> list[str]:
    """Fields that passed in the baseline and fail now. These fail the build."""
    out = []
    for fixture_id, fields in baseline.items():
        for path, passed in fields.items():
            if passed and not current.get(fixture_id, {}).get(path, False):
                out.append(f"{fixture_id}:{path}")
    return sorted(out)


def report(results_by_fixture: dict[str, dict]) -> str:
    """Per-carrier and per-field-path accuracy, as plain text."""
    by_carrier: dict[str, list[bool]] = {}
    by_path: dict[str, list[bool]] = {}
    for entry in results_by_fixture.values():
        for path, passed in entry["fields"].items():
            by_carrier.setdefault(entry["carrier"], []).append(passed)
            by_path.setdefault(path, []).append(passed)

    lines = ["", "accuracy by carrier:"]
    for carrier, values in sorted(by_carrier.items()):
        pct = 100 * sum(values) / len(values)
        lines.append(f"  {carrier:<24} {pct:5.1f}%  ({sum(values)}/{len(values)})")
    lines.append("")
    lines.append("accuracy by field path:")
    for path, values in sorted(by_path.items()):
        pct = 100 * sum(values) / len(values)
        lines.append(f"  {path:<44} {pct:5.1f}%  ({sum(values)}/{len(values)})")
    return "\n".join(lines)


def baseline_path(directory: Path, provider: str, model: str, version: str) -> Path:
    """One baseline per provider, model, and extractor version.

    A single baseline keyed by fixture alone would compare one model's results
    against another's — either failing spuriously or, worse, passing silently
    over a real regression.
    """
    slug = f"{provider}__{model}__{version}"
    return Path(directory) / (re.sub(r"[^A-Za-z0-9._-]", "-", slug) + ".json")
