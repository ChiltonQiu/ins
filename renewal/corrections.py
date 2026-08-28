"""Corrections are the long-term asset of this project.

Nothing here edits an extracted field. A correction is a new row that says what
the model produced, what the truth was, and which extractor version was
responsible. That record is what turns real usage into a labeled evaluation
set.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from renewal.models import Correction, Document, ExtractedField, Extraction

KINDS = ("wrong_value", "omission", "hallucination")


def record_correction(
    session: Session,
    *,
    extraction_id: int,
    field_path: str,
    kind: str,
    extracted_field_id: int | None = None,
    extracted_value: str | None = None,
    corrected_value: str | None = None,
    note: str | None = None,
) -> Correction:
    if kind not in KINDS:
        raise ValueError(f"unknown correction kind: {kind}")
    correction = Correction(
        extraction_id=extraction_id,
        extracted_field_id=extracted_field_id,
        field_path=field_path,
        kind=kind,
        extracted_value=extracted_value,
        corrected_value=corrected_value,
        note=note,
    )
    session.add(correction)
    session.flush()
    return correction


def effective_values(session: Session, extraction_id: int) -> dict[str, str | None]:
    """What this extraction now says, after applying every correction on it.

    Later corrections override earlier ones for the same path.
    """
    values: dict[str, str | None] = {
        field.field_path: field.value
        for field in session.query(ExtractedField)
        .filter_by(extraction_id=extraction_id)
        .order_by(ExtractedField.id)
    }
    corrections = (
        session.query(Correction)
        .filter_by(extraction_id=extraction_id)
        .order_by(Correction.id)
    )
    for correction in corrections:
        if correction.kind == "hallucination":
            values.pop(correction.field_path, None)
        else:
            values[correction.field_path] = correction.corrected_value
    return values


def export_rows(session: Session) -> list[dict]:
    """Corrections as a labeled dataset, one row per correction."""
    query = (
        session.query(Correction, Extraction, Document)
        .join(Extraction, Correction.extraction_id == Extraction.id)
        .join(Document, Extraction.document_id == Document.id)
        .order_by(Correction.id)
    )
    return [
        {
            "correction_id": correction.id,
            "blob_sha256": document.blob_sha256,
            "extractor_version": extraction.extractor_version,
            "model_id": extraction.model_id,
            "field_path": correction.field_path,
            "kind": correction.kind,
            "extracted_value": correction.extracted_value,
            "corrected_value": correction.corrected_value,
            "note": correction.note,
            "corrected_at": correction.corrected_at.isoformat(),
        }
        for correction, extraction, document in query
    ]
