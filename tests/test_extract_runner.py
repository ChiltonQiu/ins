import json
import logging

import pytest

from renewal.config import Settings
from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from renewal.models import Document, ExtractedField
from tests.pdfmaker import make_text_pdf

LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $1,840.00",
    "Bodily Injury Liability 100/300",
]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path,
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )


class FakeClient:
    """Stands in for the Anthropic API. Records what it was asked."""

    def __init__(self, response, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append({"model": model, "system": system, "content": content})
        if self.raises:
            raise self.raises
        return self.response


def _good_response():
    return json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "1840.00",
                    "confidence": 0.96,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $1,840.00",
                },
                {
                    "field_path": "coverage.BI.limit_value",
                    "value": "100/300",
                    "confidence": 0.55,
                    "source_page": 1,
                    "source_text": "Bodily Injury Liability 100/300",
                },
            ]
        }
    )


def _doc(session, store):
    return ingest_pdf(
        session, store, data=make_text_pdf([LINES]), original_filename="dec.pdf"
    )


def test_extraction_persists_fields_with_provenance(session, store, settings):
    document = _doc(session, store)
    client = FakeClient(_good_response())

    extraction = extract(
        session, store, document, "v1", client=client, settings=settings
    )

    assert extraction.status == "ok"
    assert extraction.extractor_version == "v1"
    assert extraction.model_id == "claude-opus-5"
    fields = {f.field_path: f for f in extraction.fields}
    assert fields["policy.total_premium"].value == "1840.00"
    assert fields["policy.total_premium"].source_page == 1
    assert (
        fields["policy.total_premium"].source_text_span
        == "Total Policy Premium $1,840.00"
    )


def test_low_confidence_field_is_flagged_needs_review(session, store, settings):
    document = _doc(session, store)
    extraction = extract(
        session, store, document, "v1", client=FakeClient(_good_response()),
        settings=settings,
    )
    fields = {f.field_path: f for f in extraction.fields}
    assert fields["coverage.BI.limit_value"].needs_review is True
    assert fields["policy.total_premium"].needs_review is False


def test_unverifiable_source_text_yields_partial_status(session, store, settings):
    document = _doc(session, store)
    response = json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "9999.00",
                    "confidence": 0.99,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $9,999.00",
                }
            ]
        }
    )
    extraction = extract(
        session, store, document, "v1", client=FakeClient(response), settings=settings
    )
    assert extraction.status == "partial"
    field = extraction.fields[0]
    assert field.validation_error == "source_text not found on cited page"
    assert field.confidence == 0.0
    assert field.value == "9999.00"


def test_unparseable_response_is_recorded_not_lost(session, store, settings):
    document = _doc(session, store)
    extraction = extract(
        session, store, document, "v1", client=FakeClient("could not read it"),
        settings=settings,
    )
    assert extraction.status == "invalid_response"
    assert extraction.raw_response["text"] == "could not read it"
    assert extraction.fields == []


def test_api_failure_is_recorded_not_lost(session, store, settings):
    document = _doc(session, store)
    client = FakeClient(None, raises=RuntimeError("connection reset"))
    extraction = extract(
        session, store, document, "v1", client=client, settings=settings
    )
    assert extraction.status == "failed"
    assert "connection reset" in extraction.raw_response["error"]


def test_retry_creates_a_new_extraction_and_leaves_the_old_one(
    session, store, settings
):
    document = _doc(session, store)
    first = extract(
        session, store, document, "v1", client=FakeClient("garbage"), settings=settings
    )
    second = extract(
        session, store, document, "v1", client=FakeClient(_good_response()),
        settings=settings,
    )
    assert first.id != second.id
    assert first.status == "invalid_response"
    assert second.status == "ok"


def test_text_path_sends_document_text_not_images(session, store, settings):
    document = _doc(session, store)
    client = FakeClient(_good_response())
    extract(session, store, document, "v1", client=client, settings=settings)
    content = client.calls[0]["content"]
    assert content[0]["type"] == "text"
    assert "AU-4471" in content[0]["text"]


def test_no_extracted_content_reaches_the_logs(session, store, settings, caplog):
    document = _doc(session, store)
    with caplog.at_level(logging.DEBUG):
        extract(
            session, store, document, "v1", client=FakeClient(_good_response()),
            settings=settings,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "1840.00" not in logged
    assert "AU-4471" not in logged
    assert "100/300" not in logged
    assert str(document.id) in logged


def test_missing_blob_is_recorded_as_failed_not_raised(session, store, settings):
    # A document row whose blob was never written to the store (or has since
    # gone missing) must not raise out of extract() — the attempt still gets
    # an Extraction row, same as an API failure.
    document = Document(
        blob_sha256="0" * 64,
        original_filename="missing.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()

    extraction = extract(
        session, store, document, "v1", client=FakeClient(_good_response()),
        settings=settings,
    )

    assert extraction.status == "failed"
    assert extraction.raw_response["error"]
    assert extraction.fields == []
