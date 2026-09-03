"""The bounded model pass over the front of a document."""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from pydantic import BaseModel, Field

from renewal.config import Settings
from renewal.dates import prompt_v1
from renewal.dates.regex_pass import DateCandidate
from renewal.pdftext import PageText
from renewal.providers import ModelClient, text_block

logger = logging.getLogger(__name__)

LLM_VERSION = prompt_v1.VERSION


class LlmDate(BaseModel):
    date_value: date
    date_type: str
    source_page: int = Field(ge=1)
    source_text: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_derived: bool = False
    anchor_date: date | None = None
    anchor_source_text: str | None = None


class _Payload(BaseModel):
    dates: list[LlmDate]


def parse_dates(raw: str) -> list[LlmDate]:
    """Raises when no JSON object can be recovered. Returning an empty list on
    unparseable output would be indistinguishable from a document that
    genuinely has no dates, and the caller needs to tell those apart."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return _Payload.model_validate(json.loads(match.group(0))).dates


def find_dates_llm(
    pages: list[PageText],
    candidates: list[DateCandidate],
    *,
    client: ModelClient,
    settings: Settings,
) -> list[LlmDate]:
    head = pages[: settings.date_pages]
    document_text = "\n".join(
        f"=== PAGE {p.page_number} ===\n{p.text}" for p in head
    )
    listed = "\n".join(
        f"- page {c.source_page}: {c.source_text}"
        for c in candidates
        if c.source_page <= settings.date_pages
    ) or "(none)"
    content = [
        text_block(
            prompt_v1.USER_TEMPLATE.format(
                candidates=listed, document_text=document_text
            )
        )
    ]
    raw = client.complete(
        model=settings.date_model, system=prompt_v1.SYSTEM, content=content
    )
    return parse_dates(raw)
