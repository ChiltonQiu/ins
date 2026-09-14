"""Types for carrier-specific fields.

Human-set rather than extracted. A model guessing whether an unfamiliar field
is money or text is the confident wrongness this project avoids elsewhere:
"1,200" is a premium on one form and part of a policy number on another.
Admitted status and billing type are human-set for the same reason.

The type belongs to the key, not to the term, so it lives here in one copy
rather than in a column on every row that could disagree with it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

VALUE_TYPES = ("money", "date", "integer", "text")
DEFAULT_TYPE = "text"


def load_types(path: Path) -> dict[str, str]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    types = data.get("types") or {}
    for key, value_type in types.items():
        if value_type not in VALUE_TYPES:
            raise ValueError(f"{key}: unknown value type {value_type}")
    return types
