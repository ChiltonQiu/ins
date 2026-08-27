import pytest
from pydantic import ValidationError

from renewal.extract.schema import ExtractionPayload, FieldPayload, parse_payload

RAW = """
{"fields": [
  {"field_path": "policy.total_premium", "value": "1840.00", "confidence": 0.96,
   "source_page": 1, "source_text": "Total Policy Premium $1,840.00"}
]}
"""


def test_parse_payload_reads_fields():
    payload = parse_payload(RAW)
    assert isinstance(payload, ExtractionPayload)
    assert payload.fields[0].field_path == "policy.total_premium"
    assert payload.fields[0].confidence == 0.96
    assert payload.fields[0].source_page == 1


def test_parse_payload_tolerates_prose_around_the_json():
    payload = parse_payload("Here you go:\n" + RAW + "\nHope that helps.")
    assert payload.fields[0].value == "1840.00"


def test_confidence_outside_zero_to_one_is_rejected():
    with pytest.raises(ValidationError):
        FieldPayload(
            field_path="policy.total_premium",
            value="1",
            confidence=1.4,
            source_page=1,
            source_text="x",
        )


def test_missing_source_text_is_rejected():
    with pytest.raises(ValidationError):
        FieldPayload(
            field_path="policy.total_premium",
            value="1",
            confidence=0.9,
            source_page=1,
        )


def test_unparseable_response_raises_value_error():
    with pytest.raises(ValueError):
        parse_payload("the document was unreadable")
