"""Verify that the model read what it claims to have read.

A field whose source text is not on the page it cites is kept, not discarded:
an unverifiable field is evidence about the extractor, and evidence is the
point of the corpus. It is simply given zero confidence, which sends it to
review and keeps it out of the draft.
"""

from __future__ import annotations

from dataclasses import dataclass

from renewal import fieldpath
from renewal.extract.schema import ExtractionPayload, FieldPayload
from renewal.pdftext import PdfInfo


@dataclass(frozen=True)
class ValidatedField:
    payload: FieldPayload
    confidence: float
    validation_error: str | None
    needs_review: bool


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _check(field: FieldPayload, pdf: PdfInfo) -> str | None:
    if not fieldpath.is_valid(field.field_path):
        return "unknown field_path"
    if not 1 <= field.source_page <= pdf.page_count:
        return "source_page out of range"
    if not _normalize(field.source_text):
        return "source_text is empty"
    page = pdf.pages[field.source_page - 1]
    if _normalize(field.source_text) not in _normalize(page.text):
        return "source_text not found on cited page"
    return None


def validate_fields(
    payload: ExtractionPayload, pdf: PdfInfo, threshold: float
) -> list[ValidatedField]:
    results = []
    for field in payload.fields:
        error = _check(field, pdf)
        confidence = 0.0 if error else field.confidence
        results.append(
            ValidatedField(
                payload=field,
                confidence=confidence,
                validation_error=error,
                needs_review=confidence < threshold,
            )
        )
    return results
