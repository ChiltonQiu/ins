"""Every date-shaped literal on every page, for free.

The matched string is the source text, so this pass's provenance is correct by
construction and the validation step can never reject it. That makes it the
recall floor: whatever the model misses, these are already on the calendar.

The confidence numbers here are heuristics for ordering a review queue, not
probabilities. They are never presented to the user as a likelihood.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from renewal.pdftext import PageText

REGEX_VERSION = "dates-regex-v1"

LABELLED_CONFIDENCE = 0.5
UNLABELLED_CONFIDENCE = 0.3
LABEL_WINDOW = 80

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_PATTERNS = (
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"),
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})\b"),
    re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]{2,8})\.?\s+(\d{4})\b"),
)

# Ordered: the first phrase found in the window wins, so the more specific
# labels come before the ones that are substrings of them.
_LABELS = (
    ("cancellation effective", "cancellation_effective"),
    ("date of cancellation", "cancellation_effective"),
    ("non-renewal effective", "non_renewal_effective"),
    ("nonrenewal effective", "non_renewal_effective"),
    ("expiration", "policy_expiration"),
    ("expires", "policy_expiration"),
    ("renewal due", "renewal_due"),
    ("effective", "policy_effective"),
    ("payment due", "payment_due"),
    ("amount due by", "payment_due"),
    ("inspection", "inspection_deadline"),
    ("remediation", "remediation_deadline"),
    ("audit", "audit_date"),
)


@dataclass(frozen=True)
class DateCandidate:
    date_value: date
    date_type: str
    source_page: int
    source_text: str
    confidence: float


def _from_match(pattern_index: int, groups: tuple[str, ...]) -> date | None:
    try:
        if pattern_index == 0:
            month, day, year = int(groups[0]), int(groups[1]), int(groups[2])
            year += 2000 if year < 100 else 0
        elif pattern_index == 1:
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
        elif pattern_index == 2:
            month = _MONTHS.get(groups[0][:3].lower(), 0)
            day, year = int(groups[1]), int(groups[2])
        else:
            day = int(groups[0])
            month = _MONTHS.get(groups[1][:3].lower(), 0)
            year = int(groups[2])
        return date(year, month, day)
    except ValueError:
        # A date-shaped string that is not a date. Skipped rather than stored:
        # over-extraction is wanted, but an unparseable value is not a date.
        return None


def _type_for(text: str, start: int, end: int) -> str:
    window = text[max(0, start - LABEL_WINDOW) : end].lower()
    for phrase, date_type in _LABELS:
        if phrase in window:
            return date_type
    return "other"


def find_dates(pages: list[PageText]) -> list[DateCandidate]:
    out: list[DateCandidate] = []
    for page in pages:
        for index, pattern in enumerate(_PATTERNS):
            for match in pattern.finditer(page.text):
                value = _from_match(index, match.groups())
                if value is None:
                    continue
                date_type = _type_for(page.text, match.start(), match.end())
                out.append(
                    DateCandidate(
                        date_value=value,
                        date_type=date_type,
                        source_page=page.page_number,
                        source_text=match.group(0),
                        confidence=(
                            UNLABELLED_CONFIDENCE if date_type == "other"
                            else LABELLED_CONFIDENCE
                        ),
                    )
                )
    return out
