"""One canonical field-path grammar, shared by extraction, corrections, the
diff, and the materiality rules. A rule, a correction, and an eval fixture must
all be able to name the same thing the same way.
"""

from __future__ import annotations

import re

_SEG = r"[A-Za-z0-9_-]+"
_COVERAGE_LEAF = r"(limit_value|limit_basis|deductible_value|premium)"

_PATTERNS = [
    re.compile(
        r"^policy\.(carrier_name|policy_number|effective_date|expiration_date"
        r"|total_premium)$"
    ),
    re.compile(rf"^coverage\.{_SEG}\.{_COVERAGE_LEAF}$"),
    re.compile(rf"^item\.{_SEG}\.descriptor$"),
    re.compile(rf"^item\.{_SEG}\.attributes\.{_SEG}$"),
    re.compile(rf"^item\.{_SEG}\.coverage\.{_SEG}\.{_COVERAGE_LEAF}$"),
    re.compile(rf"^forms\.{_SEG}\.edition_date$"),
]


def is_valid(path: str) -> bool:
    return any(pattern.match(path) for pattern in _PATTERNS)


def item_key(
    *,
    vin: str | None,
    year: str | None = None,
    make: str | None = None,
    model: str | None = None,
) -> str:
    """Stable identity for an insured item across terms.

    Full VIN when present; otherwise normalized year-make-model. Items whose key
    appears on only one side of a comparison are an add or a drop.
    """
    if vin:
        return vin.strip().upper()
    parts = [p for p in (year, make, model) if p]
    slug = "-".join(parts).lower()
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def glob_match(pattern: str, path: str) -> bool:
    """`*` matches exactly one segment, `**` matches any number."""
    out = []
    for token in re.split(r"(\*\*|\*)", pattern):
        if token == "**":
            out.append(r".*")
        elif token == "*":
            out.append(r"[^.]+")
        else:
            out.append(re.escape(token))
    return re.match("^" + "".join(out) + "$", path) is not None
