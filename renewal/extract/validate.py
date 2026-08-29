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


def verification_rate(fields) -> float | None:
    """Share of returned fields whose source text was found on the cited page.

    Computed on read, never stored: a stored copy could disagree with the rows
    it summarises. `None` for an extraction that returned no fields at all,
    which is a different fact from a rate of zero.

    Accepts anything carrying `validation_error` — both `ValidatedField` and the
    persisted `ExtractedField`.
    """
    fields = list(fields)
    if not fields:
        return None
    verified = sum(1 for field in fields if field.validation_error is None)
    return verified / len(fields)
