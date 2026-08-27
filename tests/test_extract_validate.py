from renewal.extract.schema import ExtractionPayload, FieldPayload
from renewal.extract.validate import validate_fields
from renewal.pdftext import read_pdf
from tests.pdfmaker import make_text_pdf

LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Total Policy Premium $1,840.00",
    "Bodily Injury Liability 100/300",
]
PDF = read_pdf(make_text_pdf([LINES]))


def _payload(**overrides):
    base = dict(
        field_path="policy.total_premium",
        value="1840.00",
        confidence=0.96,
        source_page=1,
        source_text="Total Policy Premium $1,840.00",
    )
    base.update(overrides)
    return ExtractionPayload(fields=[FieldPayload(**base)])


def test_field_with_real_source_text_validates():
    result = validate_fields(_payload(), PDF, threshold=0.80)[0]
    assert result.validation_error is None
    assert result.confidence == 0.96
    assert result.needs_review is False


def test_source_text_matching_ignores_whitespace_and_case():
    result = validate_fields(
        _payload(source_text="total policy   premium  $1,840.00"), PDF, threshold=0.80
    )[0]
    assert result.validation_error is None


def test_source_text_absent_from_page_forces_zero_confidence():
    result = validate_fields(
        _payload(source_text="Total Policy Premium $9,999.00"), PDF, threshold=0.80
    )[0]
    assert result.validation_error == "source_text not found on cited page"
    assert result.confidence == 0.0
    assert result.needs_review is True


def test_page_out_of_range_is_a_validation_error():
    result = validate_fields(_payload(source_page=7), PDF, threshold=0.80)[0]
    assert result.validation_error == "source_page out of range"
    assert result.confidence == 0.0


def test_unknown_field_path_is_a_validation_error():
    result = validate_fields(
        _payload(field_path="policy.agent_commission"), PDF, threshold=0.80
    )[0]
    assert result.validation_error == "unknown field_path"
    assert result.confidence == 0.0


def test_low_confidence_field_is_flagged_for_review():
    result = validate_fields(_payload(confidence=0.42), PDF, threshold=0.80)[0]
    assert result.validation_error is None
    assert result.needs_review is True


def test_failed_fields_are_kept_not_dropped():
    payload = ExtractionPayload(
        fields=_payload().fields + _payload(field_path="policy.agent_commission").fields
    )
    assert len(validate_fields(payload, PDF, threshold=0.80)) == 2


def test_empty_source_text_is_a_validation_error():
    result = validate_fields(_payload(source_text=""), PDF, threshold=0.80)[0]
    assert result.validation_error == "source_text is empty"
    assert result.confidence == 0.0
    assert result.needs_review is True


def test_whitespace_only_source_text_is_a_validation_error():
    result = validate_fields(_payload(source_text="   "), PDF, threshold=0.80)[0]
    assert result.validation_error == "source_text is empty"
    assert result.confidence == 0.0
    assert result.needs_review is True
