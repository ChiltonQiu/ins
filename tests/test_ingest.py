from renewal.ingest import ingest_pdf
from renewal.models import Document
from tests.pdfmaker import make_scanned_pdf, make_text_pdf

LINES = ["PROGRESSIVE PERSONAL AUTO", "Total Policy Premium $1,840.00"] * 8


def test_ingest_records_document_metadata(session, store):
    doc = ingest_pdf(
        session,
        store,
        data=make_text_pdf([LINES, LINES]),
        original_filename="renewal 2026.pdf",
    )
    assert doc.page_count == 2
    assert doc.has_text_layer is True
    assert doc.doc_type == "dec_page"
    assert doc.original_filename == "renewal 2026.pdf"
    assert len(doc.blob_sha256) == 64


def test_identical_bytes_dedup_to_one_blob_but_two_documents(session, store):
    data = make_text_pdf([LINES])
    first = ingest_pdf(session, store, data=data, original_filename="a.pdf")
    second = ingest_pdf(session, store, data=data, original_filename="b.pdf")

    assert first.id != second.id
    assert first.blob_sha256 == second.blob_sha256
    blobs = list(store.root.rglob("*.pdf"))
    assert len(blobs) == 1
    assert session.query(Document).count() == 2


def test_ingest_stores_retrievable_bytes(session, store):
    data = make_text_pdf([LINES])
    doc = ingest_pdf(session, store, data=data, original_filename="a.pdf")
    assert store.get(doc.blob_sha256) == data


def test_scanned_document_is_flagged_without_text_layer(session, store):
    doc = ingest_pdf(
        session, store, data=make_scanned_pdf([LINES]), original_filename="scan.pdf"
    )
    assert doc.has_text_layer is False
