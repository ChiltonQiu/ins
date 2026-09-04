import json

import pytest

from renewal.classify.runner import (
    CLASSIFIER_VERSION, DOC_CLASSES, classify, latest_class, parse_classification,
)
from renewal.models import DocumentClassification
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings


# Several lines, not one long one: make_text_pdf writes each string as a line
# and a long line runs off the page and is clipped. Together they clear
# MIN_CHARS_FOR_TEXT_LAYER, so the page keeps its real text layer instead of
# being routed to OCR — a short page would make these tests depend on whether
# tesseract can read one character.
PAGE = [
    "NOTICE OF CANCELLATION",
    "Issued by the carrier for the policy named below.",
    "The effective date of cancellation is stated on this page.",
]


def _document(session, store, lines):
    from renewal.pipeline import ingest_document
    return ingest_document(
        session, store, data=make_text_pdf([lines]), original_filename="d.pdf",
        source="bulk_import", agency_id=1,
        model_client=StubClient('{"dates": []}'), settings=_settings(),
    )


def test_the_class_list_matches_the_spec():
    assert DOC_CLASSES == (
        "declarations", "endorsement", "cancellation_notice",
        "non_renewal_notice", "invoice", "id_card", "loss_run",
        "inspection_report", "quote", "correspondence", "unknown",
    )


def test_parses_a_label_and_confidence():
    assert parse_classification(
        json.dumps({"doc_class": "invoice", "confidence": 0.8})
    ) == ("invoice", 0.8)


def test_an_unrecognised_label_becomes_unknown_rather_than_being_stored():
    """A label outside the set is a model error, and unknown is the honest
    result of a model error."""
    assert parse_classification(
        json.dumps({"doc_class": "renewal_thingy", "confidence": 0.9})
    ) == ("unknown", 0.0)


def test_unparseable_output_becomes_unknown_not_an_exception():
    """Classification must never fail an ingest; it gates nothing."""
    assert parse_classification("I'm not sure what that is.") == ("unknown", 0.0)


def test_classification_is_stored_with_its_version_and_model(session, store):
    document = _document(session, store, ["NOTICE OF CANCELLATION"])
    stub = StubClient(json.dumps({"doc_class": "cancellation_notice",
                                  "confidence": 0.9}))
    row = classify(session, document, client=stub, settings=_settings())
    assert row.doc_class == "cancellation_notice"
    assert row.classifier_version == CLASSIFIER_VERSION
    assert "classification-model" in row.model_id


def test_only_page_one_text_is_sent(session, store):
    document = _document(session, store, PAGE)
    stub = StubClient(json.dumps({"doc_class": "unknown", "confidence": 0.1}))
    classify(session, document, client=stub, settings=_settings())
    assert "NOTICE OF CANCELLATION" in stub.calls[0][2][0]["text"]


def test_the_classification_model_is_used(session, store):
    document = _document(session, store, PAGE)
    stub = StubClient(json.dumps({"doc_class": "unknown", "confidence": 0.1}))
    classify(session, document, client=stub, settings=_settings())
    assert stub.calls[0][0] == "classification-model"


def test_re_classification_appends_and_the_latest_wins(session, store):
    document = _document(session, store, PAGE)
    # Ingest already classified once, so the invariant is the delta: each
    # re-classification appends a row rather than replacing one.
    before = session.query(DocumentClassification).filter_by(
        document_id=document.id).count()
    classify(session, document,
             client=StubClient(json.dumps({"doc_class": "unknown",
                                           "confidence": 0.1})),
             settings=_settings())
    classify(session, document,
             client=StubClient(json.dumps({"doc_class": "cancellation_notice",
                                           "confidence": 0.9})),
             settings=_settings())
    assert session.query(DocumentClassification).filter_by(
        document_id=document.id).count() == before + 2
    assert latest_class(session, document.id) == "cancellation_notice"


def test_a_model_failure_records_unknown_rather_than_nothing(session, store):
    """An absent row and a declined answer are different facts."""
    class Exploding:
        def complete(self, **kwargs):
            raise RuntimeError("provider down")

    document = _document(session, store, PAGE)
    row = classify(session, document, client=Exploding(), settings=_settings())
    assert row.doc_class == "unknown"


def test_a_document_with_no_text_is_unknown_without_a_model_call(session, store):
    document = _document(session, store, [])
    stub = StubClient(json.dumps({"doc_class": "invoice", "confidence": 0.9}))
    row = classify(session, document, client=stub, settings=_settings())
    assert row.doc_class == "unknown"
    assert stub.calls == []
