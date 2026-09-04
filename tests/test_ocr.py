import pytest

from renewal.pdftext import rasterize_page, read_pdf
from renewal.text.ocr import TesseractUnavailable, ocr_page
from tests.pdfmaker import make_scanned_pdf, make_text_pdf

tesseract = pytest.importorskip("pytesseract")


def test_rasterize_page_returns_one_page(tmp_path):
    data = make_text_pdf([["page one"], ["page two"]])
    png = rasterize_page(data, 2)
    assert png.startswith(b"\x89PNG")


def test_rasterize_page_is_one_based_and_range_checked():
    data = make_text_pdf([["only page"]])
    with pytest.raises(ValueError):
        rasterize_page(data, 0)
    with pytest.raises(ValueError):
        rasterize_page(data, 2)


@pytest.mark.ocr
def test_ocr_recovers_text_from_a_scanned_page():
    """The whole point: a scanned page has no text layer, and search still
    has to find it."""
    data = make_scanned_pdf([["CANCELLATION NOTICE"]])
    assert not read_pdf(data).has_text_layer
    text = ocr_page(rasterize_page(data, 1))
    assert "CANCELLATION" in text.upper()


@pytest.mark.ocr
def test_ocr_of_a_blank_page_is_empty_not_an_error():
    data = make_scanned_pdf([[]])
    assert ocr_page(rasterize_page(data, 1)).strip() == ""
