import pytest

from renewal.models import Document, DocumentText
from renewal.pipeline import ingest_document
from tests.pdfmaker import make_text_pdf


def test_ingest_stores_the_document_and_its_text(session, store):
    document = ingest_document(
        session, store, data=make_text_pdf([["Named Insured: Acme"]]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
    )
    assert document.source == "bulk_import"
    assert document.agency_id == 1
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1


def test_identical_bytes_deduplicate_the_blob_but_not_the_document(session, store):
    """The same PDF arriving twice is two events, one blob."""
    data = make_text_pdf([["Named Insured: Acme"]])
    first = ingest_document(session, store, data=data, original_filename="a.pdf",
                            source="bulk_import", agency_id=1)
    second = ingest_document(session, store, data=data, original_filename="b.pdf",
                             source="email_attachment", agency_id=1)
    assert first.id != second.id
    assert first.blob_sha256 == second.blob_sha256


def test_an_unknown_source_is_rejected(session, store):
    with pytest.raises(ValueError):
        ingest_document(session, store, data=make_text_pdf([["x"]]),
                        original_filename="a.pdf", source="telepathy", agency_id=1)


def test_text_extraction_failure_does_not_lose_the_document(session, store, monkeypatch):
    """Storage must never depend on a later stage succeeding."""
    import renewal.pipeline as pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("tesseract exploded")

    monkeypatch.setattr(pipeline, "extract_text", boom)
    document = ingest_document(session, store, data=make_text_pdf([["x"]]),
                               original_filename="a.pdf", source="bulk_import",
                               agency_id=1)
    assert session.get(Document, document.id) is not None
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 0


def test_the_dates_stage_runs_and_stores_the_regex_floor(session, store):
    """No model is configured here, and dates still land: that is the point of
    the regex pass being free and unconditional."""
    from renewal.models import DocumentDate

    document = ingest_document(
        session, store, data=make_text_pdf([["Expiration Date: 07/01/2026"]]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
    )
    rows = session.query(DocumentDate).filter_by(document_id=document.id).all()
    assert [r.pass_name for r in rows] == ["regex"]
    assert rows[0].date_type == "policy_expiration"


def test_a_failing_dates_stage_does_not_lose_the_document(session, store, monkeypatch):
    import renewal.pipeline as pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("date extraction exploded")

    monkeypatch.setattr(pipeline, "extract_dates", boom)
    document = ingest_document(session, store, data=make_text_pdf([["x"]]),
                               original_filename="a.pdf", source="bulk_import",
                               agency_id=1)
    assert session.get(Document, document.id) is not None


# Several lines, not one long one: make_text_pdf writes each string as a line
# and a long line runs off the page and is clipped. Together they clear
# MIN_CHARS_FOR_TEXT_LAYER, so the page keeps its text layer rather than being
# routed to OCR and arriving at the classifier empty.
CLASSIFIABLE = [
    "DECLARATIONS PAGE",
    "Commercial auto policy for the named insured below.",
    "Coverages, limits, and the premium for this term follow.",
]


def test_ingest_classifies_the_document(session, store):
    import json

    from renewal.classify.runner import latest_class
    from tests.test_dates_llm import StubClient, _settings

    stub = StubClient(json.dumps({"doc_class": "declarations", "confidence": 0.9}))
    document = ingest_document(
        session, store, data=make_text_pdf([[CLASSIFIABLE]]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=stub, settings=_settings(),
    )
    assert latest_class(session, document.id) == "declarations"


def test_only_declarations_and_endorsements_route_to_field_extraction(session, store):
    import json

    from renewal.pipeline import should_extract_fields
    from tests.test_dates_llm import StubClient, _settings

    for label, expected in (("declarations", True), ("endorsement", True),
                            ("invoice", False), ("unknown", False)):
        stub = StubClient(json.dumps({"doc_class": label, "confidence": 0.9}))
        document = ingest_document(
            session, store, data=make_text_pdf([[f"{CLASSIFIABLE} {label}"]]),
            original_filename="d.pdf", source="bulk_import", agency_id=1,
            model_client=stub, settings=_settings(),
        )
        assert should_extract_fields(session, document.id) is expected


def test_a_failed_classification_does_not_stop_text_or_dates(session, store):
    """Classification gates nothing. This is the test that proves it."""
    from renewal.models import DocumentDate
    from tests.test_dates_llm import _settings

    class Exploding:
        def complete(self, **kwargs):
            raise RuntimeError("provider down")

    document = ingest_document(
        session, store,
        data=make_text_pdf([CLASSIFIABLE + ["Expiration Date: 07/01/2026"]]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=Exploding(), settings=_settings(),
    )
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1
    assert session.query(DocumentDate).filter_by(document_id=document.id).count() > 0
