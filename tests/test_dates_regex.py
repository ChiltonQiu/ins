from datetime import date

from renewal.dates.regex_pass import REGEX_VERSION, find_dates
from renewal.pdftext import PageText


def _pages(*texts):
    return [PageText(i + 1, t) for i, t in enumerate(texts)]


def test_slash_dates_are_read_month_first():
    """US insurance documents. 07/01/2026 is July 1st, not January 7th."""
    got = find_dates(_pages("Expiration Date: 07/01/2026"))
    assert got[0].date_value == date(2026, 7, 1)


def test_two_digit_years_resolve_to_this_century():
    got = find_dates(_pages("Effective 07/01/26"))
    assert got[0].date_value == date(2026, 7, 1)


def test_iso_dates_are_read():
    got = find_dates(_pages("Effective Date: 2026-07-01"))
    assert got[0].date_value == date(2026, 7, 1)


def test_long_form_dates_are_read():
    assert find_dates(_pages("Dated: June 1, 2026"))[0].date_value == date(2026, 6, 1)
    assert find_dates(_pages("Dated 1 June 2026"))[0].date_value == date(2026, 6, 1)
    assert find_dates(_pages("Dated Jun 1 2026"))[0].date_value == date(2026, 6, 1)


def test_source_text_is_the_matched_literal_so_validation_is_a_tautology():
    page = "Expiration Date: 07/01/2026"
    got = find_dates(_pages(page))[0]
    assert got.source_text in page


def test_page_numbers_are_carried_through():
    got = find_dates(_pages("nothing here", "Expiration Date: 07/01/2026"))
    assert got[0].source_page == 2


def test_a_nearby_label_assigns_the_type():
    got = find_dates(_pages("Cancellation Effective: 07/01/2026"))
    assert got[0].date_type == "cancellation_effective"


def test_no_label_falls_back_to_other_rather_than_guessing():
    got = find_dates(_pages("Printed 07/01/2026"))
    assert got[0].date_type == "other"
    assert got[0].confidence == 0.3


def test_a_labelled_date_carries_the_higher_heuristic_confidence():
    got = find_dates(_pages("Expiration Date: 07/01/2026"))
    assert got[0].confidence == 0.5


def test_an_impossible_date_is_skipped_not_stored():
    assert find_dates(_pages("Reference 13/45/2026")) == []


def test_over_extraction_is_the_intent():
    """Every date-shaped literal, including ones that turn out to be noise.
    A spurious date she dismisses costs two seconds."""
    got = find_dates(_pages(
        "Effective 07/01/2025 Expiration 07/01/2026 Printed 06/15/2025"))
    assert len(got) == 3


def test_the_same_literal_twice_on_a_page_yields_two_candidates():
    """Deduplication is the service's job, not this pass's; a date printed in
    two places is two pieces of evidence."""
    got = find_dates(_pages("Effective 07/01/2026 ... Effective 07/01/2026"))
    assert len(got) == 2


def test_version_is_stable():
    assert REGEX_VERSION == "dates-regex-v1"
