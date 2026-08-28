import base64
import json

import pytest

from renewal.config import Settings
from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from tests.pdfmaker import make_scanned_pdf

LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $1,840.00",
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


class CapturingClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append(content)
        return self.response


def test_scanned_document_is_sent_as_page_images(session, store, settings):
    document = ingest_pdf(
        session,
        store,
        data=make_scanned_pdf([LINES, LINES]),
        original_filename="scan.pdf",
    )
    client = CapturingClient(json.dumps({"fields": []}))

    extract(session, store, document, "v1", client=client, settings=settings)

    content = client.calls[0]
    assert content[0]["type"] == "text"
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 2
    assert images[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(images[0]["source"]["data"]).startswith(b"\x89PNG")


def test_scanned_path_returns_the_same_structure_as_the_text_path(
    session, store, settings
):
    document = ingest_pdf(
        session, store, data=make_scanned_pdf([LINES]), original_filename="scan.pdf"
    )
    response = json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "1840.00",
                    "confidence": 0.88,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $1,840.00",
                }
            ]
        }
    )
    extraction = extract(
        session, store, document, "v1", client=CapturingClient(response),
        settings=settings,
    )
    field = extraction.fields[0]
    assert field.field_path == "policy.total_premium"
    assert field.source_page == 1
    assert field.source_text_span == "Total Policy Premium $1,840.00"


def test_source_text_cannot_be_verified_on_a_scanned_page(session, store, settings):
    """There is no text layer to check against, so every field is unverifiable
    and lands in review. That is the honest outcome, not a bug to paper over."""
    document = ingest_pdf(
        session, store, data=make_scanned_pdf([LINES]), original_filename="scan.pdf"
    )
    response = json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "1840.00",
                    "confidence": 0.88,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $1,840.00",
                }
            ]
        }
    )
    extraction = extract(
        session, store, document, "v1", client=CapturingClient(response),
        settings=settings,
    )
    assert extraction.status == "partial"
    assert extraction.fields[0].needs_review is True
