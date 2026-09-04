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
