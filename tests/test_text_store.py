import pytest

from renewal.ingest import ingest_pdf
from renewal.models import DocumentText
from renewal.text.store import TEXT_VERSION, extract_text, has_text, page_text
from tests.pdfmaker import make_scanned_pdf, make_text_pdf

# A page must clear MIN_CHARS_FOR_TEXT_LAYER to be treated as having a usable
# text layer, so a realistic amount of dec-page boilerplate stands in for the
# one-line fixtures a shorter test would use.
DEC_PAGE = [
    "Named Insured: Acme Landscaping LLC",
    "Policy Number: AU-0000001   Carrier: Progressive",
    "Effective 03/01/2026 to 09/01/2026   Total Premium: 1,840.00",
]


def _ingest(session, store, data):
    return ingest_pdf(session, store, data=data, original_filename="d.pdf")


def test_a_text_layer_page_uses_pymupdf(session, store):
    document = _ingest(session, store, make_text_pdf([DEC_PAGE]))
    rows = extract_text(session, store, document)
    assert [r.extraction_method for r in rows] == ["pymupdf"]
    assert "Acme" in rows[0].text


def test_page_numbers_are_one_based(session, store):
    document = _ingest(session, store, make_text_pdf([["first"], ["second"]]))
    rows = extract_text(session, store, document)
    assert [r.page_number for r in rows] == [1, 2]
    assert "second" in page_text(session, document.id, 2)


@pytest.mark.ocr
def test_a_scanned_page_falls_back_to_ocr(session, store):
    document = _ingest(session, store, make_scanned_pdf([["CANCELLATION NOTICE"]]))
    rows = extract_text(session, store, document)
    assert rows[0].extraction_method == "ocr_tesseract"
    assert "CANCELLATION" in rows[0].text.upper()


@pytest.mark.ocr
def test_the_method_is_decided_per_page_not_per_document(session, store):
    """A digital dec page with a scanned endorsement behind it."""
    digital = make_text_pdf([DEC_PAGE])
    scanned = make_scanned_pdf([["ENDORSEMENT"]])
    import fitz
    merged = fitz.open(stream=digital, filetype="pdf")
    merged.insert_pdf(fitz.open(stream=scanned, filetype="pdf"))
    document = _ingest(session, store, merged.tobytes())
    rows = extract_text(session, store, document)
    assert [r.extraction_method for r in rows] == ["pymupdf", "ocr_tesseract"]


def test_an_empty_page_still_gets_a_row(session, store):
    document = _ingest(session, store, make_text_pdf([[]]))
    rows = extract_text(session, store, document)
    assert len(rows) == 1
    assert has_text(session, document.id) is True


def test_re_running_at_the_same_version_is_a_no_op(session, store):
    document = _ingest(session, store, make_text_pdf([["hello"]]))
    extract_text(session, store, document)
    extract_text(session, store, document)
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1


def test_has_text_is_false_before_extraction(session, store):
    document = _ingest(session, store, make_text_pdf([["hello"]]))
    assert has_text(session, document.id) is False


def test_extraction_at_a_new_version_adds_rows_rather_than_replacing(session, store):
    document = _ingest(session, store, make_text_pdf([["hello"]]))
    extract_text(session, store, document)
    extract_text(session, store, document, version="text-v2")
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 2
