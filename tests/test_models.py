import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import (
    Client,
    Correction,
    Coverage,
    Document,
    Extraction,
    InsuredItem,
    Policy,
    PolicyTerm,
)


def _policy(session):
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


def test_term_chain_persists(session):
    policy = _policy(session)
    doc = Document(
        blob_sha256="a" * 64,
        original_filename="dec.pdf",
        page_count=2,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(doc)
    session.flush()
    term = PolicyTerm(
        policy_id=policy.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        effective_date="2026-03-01",
        expiration_date="2026-09-01",
        total_premium="1840.00",
        source_document_id=doc.id,
    )
    session.add(term)
    session.flush()
    assert term.id is not None


def test_coverage_item_id_is_nullable_for_policy_level(session):
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id, effective_date="2026-03-01")
    session.add(term)
    session.flush()

    session.add(
        Coverage(
            policy_term_id=term.id,
            coverage_code="BI",
            limit_value="100/300",
        )
    )
    item = InsuredItem(
        policy_term_id=term.id, item_type="vehicle", descriptor="2018 Ford F-150"
    )
    session.add(item)
    session.flush()
    session.add(
        Coverage(
            policy_term_id=term.id,
            insured_item_id=item.id,
            coverage_code="COLL",
            deductible_value="500",
        )
    )
    session.flush()
    codes = {c.coverage_code: c.insured_item_id for c in term.coverages}
    assert codes == {"BI": None, "COLL": item.id}


def test_correction_allows_null_field_id_for_omissions(session):
    doc = Document(
        blob_sha256="b" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(doc)
    session.flush()
    extraction = Extraction(
        document_id=doc.id, extractor_version="v1", model_id="claude-opus-5", status="ok"
    )
    session.add(extraction)
    session.flush()
    session.add(
        Correction(
            extraction_id=extraction.id,
            extracted_field_id=None,
            field_path="coverage.UMBI.limit_value",
            kind="omission",
            corrected_value="100/300",
        )
    )
    session.flush()


def test_correction_kind_is_constrained(session):
    doc = Document(
        blob_sha256="c" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(doc)
    session.flush()
    extraction = Extraction(
        document_id=doc.id, extractor_version="v1", model_id="claude-opus-5", status="ok"
    )
    session.add(extraction)
    session.flush()
    session.add(
        Correction(
            extraction_id=extraction.id,
            field_path="policy.total_premium",
            kind="typo",
            corrected_value="1",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
