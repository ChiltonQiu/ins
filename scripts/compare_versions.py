"""Per-field accuracy diff between two recorded baselines.

Usage: python scripts/compare_versions.py <baseline-a.json> <baseline-b.json>

Reads files only — no API calls. The two baselines may differ in extractor
version, provider, model, or all three, which is what makes this a model
comparison as well as a version comparison.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def field_results(path: Path) -> dict[str, dict[str, bool]]:
    data = json.loads(Path(path).read_text())
    return {fixture: entry["fields"] for fixture, entry in data.items()}


def accuracy_by_path(results: dict[str, dict[str, bool]]) -> dict[str, float]:
    totals: dict[str, list[bool]] = {}
    for fields in results.values():
        for field_path, passed in fields.items():
            totals.setdefault(field_path, []).append(passed)
    return {
        field_path: 100 * sum(values) / len(values)
        for field_path, values in totals.items()
    }


def main(path_a: str, path_b: str) -> None:
    a = accuracy_by_path(field_results(Path(path_a)))
    b = accuracy_by_path(field_results(Path(path_b)))
    label_a, label_b = Path(path_a).stem, Path(path_b).stem
    width = max(24, len(label_a), len(label_b))

    print(f"{'field path':<44} {label_a:>{width}} {label_b:>{width}}   delta")
    for field_path in sorted(set(a) | set(b)):
        a_pct, b_pct = a.get(field_path, 0.0), b.get(field_path, 0.0)
        if a_pct != b_pct:
            print(
                f"{field_path:<44} {a_pct:{width - 1}.1f}% {b_pct:{width - 1}.1f}% "
                f" {b_pct - a_pct:+6.1f}"
            )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
