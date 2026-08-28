import datetime as dt

import pytest

from renewal.corrections import record_correction
from renewal.models import (
    Client,
    Coverage,
    Document,
    ExtractedField,
    Extraction,
    InsuredItem,
    Policy,
)
from renewal.promote import PromotionBlocked, promote, unresolved_field_paths

FIELDS = {
    "policy.carrier_name": ("Progressive", 0.99),
    "policy.policy_number": ("AU-4471", 0.99),
    "policy.effective_date": ("2026-03-01", 0.98),
    "policy.expiration_date": ("2026-09-01", 0.98),
    "policy.total_premium": ("1840.00", 0.97),
    "coverage.BI.limit_value": ("100/300", 0.95),
    "item.VIN0001.descriptor": ("2018 Ford F-150", 0.95),
    "item.VIN0001.attributes.garaging_zip": ("78704", 0.92),
    "item.VIN0001.coverage.COLL.deductible_value": ("500", 0.94),
    "item.VIN0001.coverage.COLL.premium": ("412.00", 0.93),
    "forms.A085.edition_date": ("2019-06", 0.90),
}


@pytest.fixture
def policy(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    return policy


@pytest.fixture
def extraction(session):
    document = Document(
        blob_sha256="e" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    extraction = Extraction(
        document_id=document.id,
        extractor_version="v1",
        model_id="claude-opus-5",
        status="ok",
    )
    session.add(extraction)
    session.flush()
    for path, (value, confidence) in FIELDS.items():
        session.add(
            ExtractedField(
                extraction_id=extraction.id,
                field_path=path,
                value=value,
                confidence=confidence,
                source_page=1,
                source_text_span=value,
                needs_review=confidence < 0.80,
            )
        )
    session.flush()
    return extraction


def test_promotion_writes_a_term_with_policy_scalars(session, policy, extraction):
    term = promote(session, extraction, policy.id)
    assert term.policy_id == policy.id
    assert term.carrier_name == "Progressive"
    assert term.policy_number == "AU-4471"
    assert term.effective_date == dt.date(2026, 3, 1)
    assert term.expiration_date == dt.date(2026, 9, 1)
    assert term.total_premium == "1840.00"
    assert term.promoted_from_extraction_id == extraction.id
    assert term.source_document_id == extraction.document_id


def test_policy_level_and_vehicle_level_coverages_land_correctly(
    session, policy, extraction
):
    term = promote(session, extraction, policy.id)
    coverages = {c.coverage_code: c for c in term.coverages}
    assert coverages["BI"].insured_item_id is None
    assert coverages["BI"].limit_value == "100/300"

    vehicle = next(i for i in term.items if i.item_type == "vehicle")
    assert vehicle.descriptor == "2018 Ford F-150"
    assert vehicle.attributes["item_key"] == "VIN0001"
    assert vehicle.attributes["garaging_zip"] == "78704"
    assert coverages["COLL"].insured_item_id == vehicle.id
    assert coverages["COLL"].deductible_value == "500"
    assert coverages["COLL"].premium == "412.00"


def test_forms_are_promoted_so_the_diff_can_see_them(session, policy, extraction):
    term = promote(session, extraction, policy.id)
    form = next(i for i in term.items if i.item_type == "form")
    assert form.descriptor == "A085"
    assert form.attributes["edition_date"] == "2019-06"


def test_corrections_are_applied_at_promotion(session, policy, extraction):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="policy.total_premium")
        .one()
    )
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value=field.value,
        corrected_value="1804.00",
    )
    term = promote(session, extraction, policy.id)
    assert term.total_premium == "1804.00"


def test_promotion_is_blocked_by_unresolved_low_confidence_fields(
    session, policy, extraction
):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="coverage.BI.limit_value")
        .one()
    )
    field.needs_review = True
    session.flush()
    with pytest.raises(PromotionBlocked) as excinfo:
        promote(session, extraction, policy.id)
    assert "coverage.BI.limit_value" in excinfo.value.paths


def test_a_correction_resolves_the_block(session, policy, extraction):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="coverage.BI.limit_value")
        .one()
    )
    field.needs_review = True
    session.flush()
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value=field.value,
        corrected_value="250/500",
    )
    assert unresolved_field_paths(session, extraction.id) == []
    assert promote(session, extraction, policy.id) is not None


def test_acknowledging_a_field_also_resolves_the_block(session, policy, extraction):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="coverage.BI.limit_value")
        .one()
    )
    field.needs_review = True
    session.flush()
    assert unresolved_field_paths(
        session, extraction.id, acknowledged=frozenset({"coverage.BI.limit_value"})
    ) == []


def test_promoting_twice_creates_a_second_term_and_keeps_the_first(
    session, policy, extraction
):
    first = promote(session, extraction, policy.id)
    second = promote(session, extraction, policy.id)
    assert first.id != second.id
    assert session.get(type(first), first.id).total_premium == "1840.00"


def test_attribute_values_round_trip_as_strings(session, policy, extraction):
    """attributes is JSONB, so Python would happily store a native int or bool.
    Every value written into it comes from effective_values, which yields
    str | None, so it must round-trip as a string, not a JSON number/bool."""
    term = promote(session, extraction, policy.id)
    vehicle = next(i for i in term.items if i.item_type == "vehicle")
    assert isinstance(vehicle.attributes["garaging_zip"], str)
    assert vehicle.attributes["garaging_zip"] == "78704"
