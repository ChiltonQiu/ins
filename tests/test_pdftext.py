from renewal.pdftext import layout_text, rasterize, read_pdf
from tests.pdfmaker import make_scanned_pdf, make_text_pdf

DEC_LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471   Effective 03/01/2026 to 09/01/2026",
    "Total Policy Premium $1,840.00",
    "Bodily Injury Liability 100/300",
    "2018 Ford F-150 VIN 1FTEW1EP0JKD00001 Collision Deductible $500",
]


def test_read_pdf_reports_page_count_and_one_based_pages():
    info = read_pdf(make_text_pdf([DEC_LINES, ["Page two"]]))
    assert info.page_count == 2
    assert [p.page_number for p in info.pages] == [1, 2]


def test_text_layer_detected_when_text_is_present():
    info = read_pdf(make_text_pdf([DEC_LINES]))
    assert info.has_text_layer is True
    assert "AU-4471" in info.pages[0].text


def test_scanned_pdf_has_no_text_layer():
    info = read_pdf(make_scanned_pdf([DEC_LINES]))
    assert info.has_text_layer is False


def test_layout_text_delimits_pages():
    text = layout_text(read_pdf(make_text_pdf([DEC_LINES, ["Page two"]])))
    assert "=== PAGE 1 ===" in text
    assert "=== PAGE 2 ===" in text
    assert text.index("=== PAGE 1 ===") < text.index("=== PAGE 2 ===")


def test_rasterize_returns_one_png_per_page():
    pngs = rasterize(make_text_pdf([DEC_LINES, ["Page two"]]), dpi=100)
    assert len(pngs) == 2
    assert all(png.startswith(b"\x89PNG") for png in pngs)
