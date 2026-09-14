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


def test_only_extractable_classes_route_to_field_extraction(session, store):
    """quote joined the routed classes when the comparison engine learned to
    set a quote beside a renewal. Before that it was classified and stored and
    nothing consumed it — and a quote that is never extracted cannot become a
    comparison column."""
    import json

    from renewal.pipeline import should_extract_fields
    from tests.test_dates_llm import StubClient, _settings

    for label, expected in (("declarations", True), ("endorsement", True),
                            ("quote", True),
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


def _stage_of(model):
    """Which stage is asking. The model name is the one unambiguous marker:
    the extraction prompt talks about effective_date and expiration_date, so
    sniffing the system text puts it in the dates bucket."""
    from tests.test_dates_llm import _settings

    settings = _settings()
    if model == settings.date_model:
        return "dates"
    if model == settings.classification_model:
        return "classify"
    return "extract"


_EXTRACTION = {
    "fields": [
        {
            "field_path": "policy.total_premium",
            "value": "1840.00",
            "confidence": 0.95,
            "source_page": 1,
            "source_text": "Total Policy Premium $1,840.00",
        }
    ]
}


class Recording:
    """A client that records which stage asked it."""

    def __init__(self, calls, doc_class="declarations"):
        self.calls = calls
        self.doc_class = doc_class

    def complete(self, *, model, system, content):
        import json

        stage = _stage_of(model)
        self.calls.append(stage)
        if stage == "classify":
            return json.dumps({"doc_class": self.doc_class, "confidence": 0.9})
        if stage == "dates":
            return json.dumps({"dates": []})
        return json.dumps(_EXTRACTION)


def test_ingest_runs_the_field_stage_for_a_declarations_document(session, store):
    from renewal.models import Extraction
    from tests.test_dates_llm import _settings

    calls = []
    document = ingest_document(
        session, store, data=make_text_pdf([CLASSIFIABLE]),
        original_filename="d.pdf", source="manual_upload", agency_id=1,
        model_client=Recording(calls), settings=_settings(),
    )
    assert "extract" in calls
    assert session.query(Extraction).filter_by(document_id=document.id).count() == 1


def test_ingest_skips_the_field_stage_when_it_is_switched_off(session, store):
    """Import is for getting documents in. Extraction is a pure function of
    (blob, extractor_version) and can be re-run at any time."""
    from renewal.models import Extraction
    from tests.test_dates_llm import _settings

    calls = []
    document = ingest_document(
        session, store, data=make_text_pdf([CLASSIFIABLE]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        extract_fields=False,
        model_client=Recording(calls), settings=_settings(),
    )
    assert "extract" not in calls
    assert session.query(Extraction).filter_by(document_id=document.id).count() == 0


def test_an_unrouted_class_costs_no_extraction_call(session, store):
    from tests.test_dates_llm import _settings

    calls = []
    ingest_document(
        session, store, data=make_text_pdf([CLASSIFIABLE]),
        original_filename="d.pdf", source="manual_upload", agency_id=1,
        model_client=Recording(calls, doc_class="invoice"), settings=_settings(),
    )
    assert "extract" not in calls


def test_a_failed_field_stage_does_not_lose_the_document(session, store):
    """Every stage after storage is best-effort. A provider outage must not
    cost the import."""
    import json

    from renewal.models import DocumentText

    from tests.test_dates_llm import _settings

    class HalfDown:
        """Only the extraction model is down."""

        def complete(self, *, model, system, content):
            stage = _stage_of(model)
            if stage == "classify":
                return json.dumps({"doc_class": "declarations", "confidence": 0.9})
            if stage == "dates":
                return json.dumps({"dates": []})
            raise RuntimeError("provider down")

    document = ingest_document(
        session, store, data=make_text_pdf([CLASSIFIABLE]),
        original_filename="d.pdf", source="manual_upload", agency_id=1,
        model_client=HalfDown(), settings=_settings(),
    )
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1
