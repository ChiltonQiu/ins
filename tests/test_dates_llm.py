import json
from datetime import date

import pytest

from renewal.config import Settings
from renewal.dates.llm_pass import LLM_VERSION, find_dates_llm, parse_dates
from renewal.pdftext import PageText


class StubClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append((model, system, content))
        return self.response


def _settings(**kwargs):
    base = dict(
        database_url="", blob_root="blobs", anthropic_api_key="",
        extraction_model="m", draft_model="m", confidence_threshold=0.8,
        materiality_config="config/materiality.yaml", date_model="date-model",
        date_pages=3,
    )
    base.update(kwargs)
    return Settings(**base)


def test_parses_a_plain_date():
    raw = json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "policy_expiration",
        "source_page": 1, "source_text": "Expiration Date: 07/01/2026",
        "confidence": 0.9}]})
    got = parse_dates(raw)
    assert got[0].date_value == date(2026, 7, 1)
    assert got[0].is_derived is False


def test_parses_a_derived_date_with_its_anchor():
    raw = json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "cancellation_effective",
        "source_page": 1,
        "source_text": "within 30 days of the date of this notice",
        "confidence": 0.7, "is_derived": True,
        "anchor_date": "2026-06-01", "anchor_source_text": "Dated: June 1, 2026"}]})
    got = parse_dates(raw)[0]
    assert got.is_derived is True
    assert got.anchor_date == date(2026, 6, 1)


def test_prose_around_the_json_is_tolerated():
    raw = 'Here you go:\n{"dates": []}\nHope that helps.'
    assert parse_dates(raw) == []


def test_unparseable_output_raises_rather_than_returning_nothing():
    """Silently returning no dates would look identical to a document that
    genuinely has none."""
    with pytest.raises(ValueError):
        parse_dates("I could not read that document.")


def test_only_the_first_n_pages_are_sent():
    stub = StubClient('{"dates": []}')
    pages = [PageText(i + 1, f"page {i + 1}") for i in range(10)]
    find_dates_llm(pages, [], client=stub, settings=_settings(date_pages=3))
    sent = stub.calls[0][2][0]["text"]
    assert "page 3" in sent
    assert "page 4" not in sent


def test_regex_candidates_are_offered_to_the_model_for_typing():
    from renewal.dates.regex_pass import DateCandidate
    stub = StubClient('{"dates": []}')
    candidate = DateCandidate(date(2026, 7, 1), "other", 1, "07/01/2026", 0.3)
    find_dates_llm([PageText(1, "07/01/2026")], [candidate],
                   client=stub, settings=_settings())
    assert "07/01/2026" in stub.calls[0][2][0]["text"]


def test_the_date_model_is_used_not_the_extraction_model():
    stub = StubClient('{"dates": []}')
    find_dates_llm([PageText(1, "x")], [], client=stub, settings=_settings())
    assert stub.calls[0][0] == "date-model"


def test_version_is_stable():
    assert LLM_VERSION == "dates-llm-v1"
