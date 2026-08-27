"""The strict JSON contract the model must answer in.

Every field carries its own provenance. A field without source text cannot be
verified, and an unverifiable field is worthless — so source_text is required
here rather than optional, and a response that omits it fails to parse.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field


class FieldPayload(BaseModel):
    field_path: str
    value: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_page: int = Field(ge=1)
    source_text: str


class ExtractionPayload(BaseModel):
    fields: list[FieldPayload]


def parse_payload(raw: str) -> ExtractionPayload:
    """Parse the model's response, tolerating prose wrapped around the JSON.

    Raises ValueError when no JSON object can be recovered; the caller records
    that as an invalid_response extraction rather than losing the attempt.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return ExtractionPayload.model_validate(json.loads(match.group(0)))
