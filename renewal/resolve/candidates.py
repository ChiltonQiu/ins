"""Identifiers pulled from page text, before any matching happens.

Label-driven and deliberately literal. A missing label yields nothing rather
than a guess: an invented named insured would feed a confident wrong match, and
a wrong match files a cancellation notice under the wrong client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INSURED = re.compile(
    r"(?:named\s+insured|insured\s+name|insured)\s*[:\-]\s*(.+)", re.IGNORECASE
)
_POLICY_NUMBER = re.compile(
    r"policy\s*(?:number|no\.?|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{4,24})",
    re.IGNORECASE,
)
_CITY_STATE_ZIP = re.compile(r".+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$")


@dataclass(frozen=True)
class Candidates:
    named_insured: str | None = None
    policy_numbers: list[str] = field(default_factory=list)
    address_lines: list[str] = field(default_factory=list)


def extract_candidates(page_text: str) -> Candidates:
    lines = [line.strip() for line in page_text.splitlines()]

    insured = None
    for line in lines:
        found = _INSURED.search(line)
        if found and found.group(1).strip():
            insured = found.group(1).strip()
            break

    numbers = [n.upper() for n in _POLICY_NUMBER.findall(page_text)]
    addresses = [line for line in lines if _CITY_STATE_ZIP.match(line)]
    return Candidates(
        named_insured=insured,
        policy_numbers=list(dict.fromkeys(numbers)),
        address_lines=addresses,
    )
