from renewal.pdftext import read_pdf
from scripts.redact import redact_pdf, redact_text
from tests.pdfmaker import make_text_pdf

SUBS = {"Acme Landscaping LLC": "Example Client LLC", "CAP-7781-22": "POL-0000-00"}


def test_named_values_are_replaced():
    got = redact_text("Named Insured: Acme Landscaping LLC", substitutions=SUBS)
    assert "Acme" not in got
    assert "Example Client LLC" in got


def test_vins_are_replaced_without_being_named():
    got = redact_text("VIN 1HGCM82633A004352", substitutions={})
    assert "1HGCM82633A004352" not in got


def test_ssn_shaped_numbers_are_replaced_without_being_named():
    got = redact_text("Tax ID 123-45-6789", substitutions={})
    assert "123-45-6789" not in got


def test_layout_survives_regeneration():
    data = make_text_pdf([["Named Insured: Acme Landscaping LLC",
                           "Policy Number: CAP-7781-22"]])
    out = redact_pdf(data, substitutions=SUBS)
    text = read_pdf(out).pages[0].text
    assert "Example Client LLC" in text
    assert "POL-0000-00" in text
    assert "Acme" not in text


def test_page_count_is_preserved():
    data = make_text_pdf([["one"], ["two"]])
    assert read_pdf(redact_pdf(data, substitutions={})).page_count == 2
