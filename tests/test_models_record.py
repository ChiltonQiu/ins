from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError

from renewal.models import (
    Agency,
    AttentionEvent,
    AttentionItem,
    Carrier,
    CarrierAdmittedStatus,
    CarrierAlias,
    Client,
    DateEvent,
    Document,
    DocumentClassification,
    DocumentDate,
    DocumentLink,
    DocumentText,
    InboundMessage,
    ManualDate,
    ManualDateEvent,
    Policy,
    PolicyBillingType,
    PolicyTerm,
)


def test_agency_row_exists_with_a_seeded_default(session):
    agency = session.query(Agency).filter_by(slug="default").one()
    assert agency.ics_token
    assert len(agency.ics_token) >= 32


def test_admitted_status_is_keyed_per_state(session):
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    session.add(carrier)
    session.flush()
    session.add_all([
        CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                              status="non_admitted", set_by="human"),
        CarrierAdmittedStatus(carrier_id=carrier.id, state="AZ",
                              status="admitted", set_by="human"),
    ])
    session.flush()
    rows = {r.state: r.status for r in session.query(CarrierAdmittedStatus).all()}
    assert rows == {"CA": "non_admitted", "AZ": "admitted"}


def test_admitted_status_is_append_only_latest_wins(session):
    """Correcting a status inserts; it never updates."""
    carrier = Carrier(display_name="Travelers")
    session.add(carrier)
    session.flush()
    session.add(CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                                      status="unknown", set_by="human"))
    session.flush()
    session.add(CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                                      status="admitted", set_by="human"))
    session.flush()
    rows = session.query(CarrierAdmittedStatus).order_by(
        CarrierAdmittedStatus.id).all()
    assert [r.status for r in rows] == ["unknown", "admitted"]


def test_carrier_alias_resolves_a_name_variant(session):
    carrier = Carrier(display_name="Progressive Casualty Ins Co")
    session.add(carrier)
    session.flush()
    session.add(CarrierAlias(carrier_id=carrier.id, alias="progressive"))
    session.flush()
    found = session.query(CarrierAlias).filter_by(alias="progressive").one()
    assert found.carrier_id == carrier.id


def _policy(session):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name="Travelers",
                    policy_number="P-1", line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    return policy


def test_billing_type_defaults_to_unknown_by_absence(session):
    """No row means unknown. unknown is preferable to a guess."""
    policy = _policy(session)
    assert session.query(PolicyBillingType).filter_by(policy_id=policy.id).count() == 0


def test_billing_type_is_append_only_latest_wins(session):
    policy = _policy(session)
    session.add(PolicyBillingType(policy_id=policy.id, billing_type="unknown",
                                  set_by="human"))
    session.flush()
    session.add(PolicyBillingType(policy_id=policy.id, billing_type="direct_bill",
                                  set_by="human"))
    session.flush()
    rows = session.query(PolicyBillingType).order_by(PolicyBillingType.id).all()
    assert [r.billing_type for r in rows] == ["unknown", "direct_bill"]


def test_policy_term_carries_the_extracted_billing_type(session):
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id, billing_type="agency_bill")
    session.add(term)
    session.flush()
    assert session.get(PolicyTerm, term.id).billing_type == "agency_bill"


def test_policy_term_billing_type_may_be_null(session):
    """Only declarations and endorsements get structured extraction, so most
    terms will never have a value here."""
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id)
    session.add(term)
    session.flush()
    assert session.get(PolicyTerm, term.id).billing_type is None


def _document(session, agency_id=1):
    document = Document(blob_sha256="a" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=agency_id)
    session.add(document)
    session.flush()
    return document


def test_text_rows_are_unique_per_page_and_version(session):
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1,
                             text="hello", extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.flush()
    session.add(DocumentText(document_id=document.id, page_number=1,
                             text="hello again", extraction_method="pymupdf",
                             extractor_version="text-v1"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_page_may_be_re_extracted_at_a_new_version(session):
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1, text="a",
                             extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.add(DocumentText(document_id=document.id, page_number=1, text="b",
                             extraction_method="ocr_tesseract",
                             extractor_version="text-v2"))
    session.flush()
    assert session.query(DocumentText).count() == 2


def test_tsvector_is_populated_automatically(session):
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1,
                             text="cancellation effective July 2026",
                             extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.flush()
    found = session.execute(sql(
        "SELECT count(*) FROM document_text "
        "WHERE tsv @@ websearch_to_tsquery('english', 'cancellation')"
    )).scalar()
    assert found == 1


def test_an_empty_page_still_gets_a_row(session):
    """So the skip logic can tell 'processed, nothing there' from 'not yet
    processed'."""
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1, text="",
                             extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.flush()
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1


def test_classification_is_appended_not_updated(session):
    document = _document(session)
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="unknown", confidence=0.2,
                                       classifier_version="classify-v1",
                                       model_id="anthropic:haiku"))
    session.flush()
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="cancellation_notice",
                                       confidence=0.9,
                                       classifier_version="classify-v2",
                                       model_id="anthropic:haiku"))
    session.flush()
    rows = session.query(DocumentClassification).order_by(
        DocumentClassification.id).all()
    assert [r.doc_class for r in rows] == ["unknown", "cancellation_notice"]


def test_unmatched_is_the_absence_of_a_link(session):
    document = _document(session)
    assert session.query(DocumentLink).filter_by(document_id=document.id).count() == 0


def test_manual_assignment_appends_and_carries_the_candidates_offered(session):
    """The superseded auto row plus this candidates list is the Correction
    equivalent: what was offered, and what was right."""
    document = _document(session)
    client = Client(display_name="Acme Landscaping LLC")
    other = Client(display_name="Acme Landscaping Inc")
    session.add_all([client, other])
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=other.id,
                             method="auto", confidence=1.0, candidates=[]))
    session.flush()
    session.add(DocumentLink(
        document_id=document.id, client_id=client.id, method="manual",
        confidence=1.0,
        candidates=[{"client_id": other.id, "score": 0.91},
                    {"client_id": client.id, "score": 0.88}],
    ))
    session.flush()
    rows = session.query(DocumentLink).order_by(DocumentLink.id).all()
    assert [r.method for r in rows] == ["auto", "manual"]
    assert rows[-1].candidates[0]["score"] == 0.91


def test_a_link_may_have_no_policy(session):
    """She can know the client without knowing which policy the document is for."""
    document = _document(session)
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    link = DocumentLink(document_id=document.id, client_id=client.id,
                        policy_id=None, method="manual", confidence=1.0,
                        candidates=[])
    session.add(link)
    session.flush()
    assert session.get(DocumentLink, link.id).policy_id is None


def test_a_date_stores_its_provenance(session):
    document = _document(session)
    row = DocumentDate(
        document_id=document.id, date_value=date(2026, 7, 1),
        date_type="policy_expiration", source_page=2,
        source_text="Expiration Date: 07/01/2026", confidence=0.5,
        extractor_version="dates-regex-v1", pass_name="regex",
    )
    session.add(row)
    session.flush()
    stored = session.get(DocumentDate, row.id)
    assert stored.source_page == 2
    assert stored.source_text == "Expiration Date: 07/01/2026"


def test_a_date_has_no_client_id(session):
    """Derived through the document's latest link instead, so a re-link moves
    it with no backfill."""
    assert not hasattr(DocumentDate, "client_id")


def test_a_date_is_unconfirmed_until_an_event_says_otherwise(session):
    document = _document(session)
    row = DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                       date_type="other", source_page=1, source_text="07/01/2026",
                       confidence=0.3, extractor_version="dates-regex-v1",
                       pass_name="regex")
    session.add(row)
    session.flush()
    assert session.query(DateEvent).filter_by(document_date_id=row.id).count() == 0


def test_confirming_appends_an_event_and_leaves_the_date_untouched(session):
    document = _document(session)
    row = DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                       date_type="policy_expiration", source_page=1,
                       source_text="07/01/2026", confidence=0.5,
                       extractor_version="dates-regex-v1", pass_name="regex")
    session.add(row)
    session.flush()
    session.add(DateEvent(document_date_id=row.id, action="confirmed",
                          actor="human"))
    session.add(DateEvent(document_date_id=row.id, action="dismissed",
                          actor="human"))
    session.flush()
    events = session.query(DateEvent).order_by(DateEvent.id).all()
    assert [e.action for e in events] == ["confirmed", "dismissed"]


def test_a_derived_date_records_its_arithmetic(session):
    document = _document(session)
    row = DocumentDate(
        document_id=document.id, date_value=date(2026, 7, 1),
        date_type="cancellation_effective", source_page=1,
        source_text="within 30 days of the date of this notice",
        confidence=0.6, extractor_version="dates-llm-v1", pass_name="llm",
        is_derived=True, anchor_date=date(2026, 6, 1),
        anchor_source_text="Dated: June 1, 2026",
    )
    session.add(row)
    session.flush()
    stored = session.get(DocumentDate, row.id)
    assert stored.is_derived is True
    assert stored.anchor_date == date(2026, 6, 1)


def test_a_derived_date_may_have_an_unverified_anchor(session):
    """Stored and flagged rather than dropped; the UI warns instead."""
    document = _document(session)
    row = DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                       date_type="cancellation_effective", source_page=1,
                       source_text="within 30 days of this notice",
                       confidence=0.4, extractor_version="dates-llm-v1",
                       pass_name="llm", is_derived=True, anchor_date=None,
                       anchor_source_text=None)
    session.add(row)
    session.flush()
    assert session.get(DocumentDate, row.id).anchor_date is None


def test_a_manual_date_needs_no_document(session):
    """The only way this replaces the handwritten list."""
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    row = ManualDate(agency_id=1, client_id=client.id, title="Call about audit",
                     date_value=date(2026, 9, 1), date_type="audit_date",
                     created_by="human")
    session.add(row)
    session.flush()
    session.add(ManualDateEvent(manual_date_id=row.id, action="dismissed",
                                actor="human"))
    session.flush()
    assert session.query(ManualDateEvent).count() == 1


def test_message_id_is_unique_per_agency(session):
    """Forwarded mail arrives multiple times; the second arrival does no work."""
    for _ in range(2):
        session.add(InboundMessage(
            agency_id=1, message_id="<abc@carrier.example>",
            from_address="underwriting@carrier.example",
            to_address="intake+default@example.com", subject="Cancellation",
            received_at=datetime.now(timezone.utc),
            raw_mime_blob_sha256="b" * 64, body_text="",
            processing_status="received",
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_quarantined_mail_is_stored_not_dropped(session):
    message = InboundMessage(
        agency_id=1, message_id="<x@y>", from_address="stranger@example.com",
        to_address="intake+nobody@example.com", subject="hi",
        received_at=datetime.now(timezone.utc), raw_mime_blob_sha256="c" * 64,
        body_text="", processing_status="quarantined",
    )
    session.add(message)
    session.flush()
    assert session.get(InboundMessage, message.id).processing_status == "quarantined"


def test_attention_item_has_no_client_id(session):
    """Derived through the document's latest link, same as dates."""
    assert not hasattr(AttentionItem, "client_id")


def test_resolving_an_item_appends_an_event(session):
    document = _document(session)
    item = AttentionItem(document_id=document.id, reason_code="cancellation_notice",
                         reason_text="Classified as a cancellation notice")
    session.add(item)
    session.flush()
    session.add(AttentionEvent(attention_item_id=item.id, action="done",
                               actor="human"))
    session.flush()
    assert session.query(AttentionEvent).filter_by(action="done").count() == 1
