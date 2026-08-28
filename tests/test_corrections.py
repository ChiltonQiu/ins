import json

import pytest

from renewal.corrections import effective_values, export_rows, record_correction
from renewal.models import Correction, Document, ExtractedField, Extraction


@pytest.fixture
def extraction(session):
    document = Document(
        blob_sha256="d" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    extraction = Extraction(
        document_id=document.id,
        extractor_version="v1",
        model_id="claude-opus-5",
        status="ok",
    )
    session.add(extraction)
    session.flush()
    session.add_all(
        [
            ExtractedField(
                extraction_id=extraction.id,
                field_path="policy.total_premium",
                value="1840.00",
                confidence=0.96,
                source_page=1,
                source_text_span="Total Policy Premium $1,840.00",
            ),
            ExtractedField(
                extraction_id=extraction.id,
                field_path="coverage.BI.limit_value",
                value="100/300",
                confidence=0.91,
                source_page=1,
                source_text_span="Bodily Injury Liability 100/300",
            ),
        ]
    )
    session.flush()
    return extraction


def _field(session, extraction, path):
    return (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path=path)
        .one()
    )


def test_uncorrected_extraction_returns_extracted_values(session, extraction):
    assert effective_values(session, extraction.id) == {
        "policy.total_premium": "1840.00",
        "coverage.BI.limit_value": "100/300",
    }


def test_wrong_value_correction_replaces_without_touching_the_field(
    session, extraction
):
    field = _field(session, extraction, "policy.total_premium")
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value=field.value,
        corrected_value="1804.00",
    )
    assert effective_values(session, extraction.id)["policy.total_premium"] == "1804.00"
    session.refresh(field)
    assert field.value == "1840.00"  # the extracted row is never edited


def test_omission_adds_a_field_the_model_never_emitted(session, extraction):
    record_correction(
        session,
        extraction_id=extraction.id,
        field_path="coverage.UMBI.limit_value",
        kind="omission",
        corrected_value="100/300",
    )
    values = effective_values(session, extraction.id)
    assert values["coverage.UMBI.limit_value"] == "100/300"


def test_hallucination_removes_a_field_that_is_not_on_the_document(
    session, extraction
):
    field = _field(session, extraction, "coverage.BI.limit_value")
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="hallucination",
        extracted_value=field.value,
    )
    assert "coverage.BI.limit_value" not in effective_values(session, extraction.id)


def test_latest_correction_wins_and_earlier_ones_are_kept(session, extraction):
    field = _field(session, extraction, "policy.total_premium")
    for value in ("1804.00", "1840.50"):
        record_correction(
            session,
            extraction_id=extraction.id,
            extracted_field_id=field.id,
            field_path=field.field_path,
            kind="wrong_value",
            extracted_value=field.value,
            corrected_value=value,
        )
    assert effective_values(session, extraction.id)["policy.total_premium"] == "1840.50"
    assert session.query(Correction).count() == 2


def test_export_rows_emit_all_three_kinds_with_the_extractor_version(
    session, extraction
):
    field = _field(session, extraction, "policy.total_premium")
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value="1840.00",
        corrected_value="1804.00",
    )
    record_correction(
        session,
        extraction_id=extraction.id,
        field_path="coverage.UMBI.limit_value",
        kind="omission",
        corrected_value="100/300",
    )
    rows = export_rows(session)
    assert {row["kind"] for row in rows} == {"wrong_value", "omission"}
    assert all(row["extractor_version"] == "v1" for row in rows)
    assert all(row["blob_sha256"] == "d" * 64 for row in rows)
    json.dumps(rows)  # must be serializable as-is
