from datetime import date

from renewal.calendarview.agenda import ALL_STATUSES, agenda
from renewal.dates.service import confirm, dismiss
from renewal.models import (
    Client, DocumentClassification, DocumentDate, DocumentLink, ManualDate,
)


def _document(session):
    from renewal.models import Document
    document = Document(blob_sha256="a" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    return document


def _dated(session, document, value, date_type, **kwargs):
    row = DocumentDate(document_id=document.id, date_value=value,
                       date_type=date_type, source_page=1, source_text="x",
                       confidence=0.5, extractor_version="dates-regex-v1",
                       pass_name="regex", **kwargs)
    session.add(row)
    session.flush()
    return row


def _linked(session, document, name="Acme Landscaping LLC"):
    client = Client(display_name=name)
    session.add(client)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="auto", confidence=1.0, candidates=[]))
    session.flush()
    return client


def test_an_extracted_date_appears_with_its_client(session):
    document = _document(session)
    client = _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    entries = agenda(session, agency_id=1)
    assert entries[0].client_name == client.display_name
    assert entries[0].status == "unconfirmed"


def test_a_relinked_document_moves_its_dates_with_no_backfill(session):
    """The reason document_date carries no client_id."""
    document = _document(session)
    _linked(session, document, "Wrong Client LLC")
    right = Client(display_name="Right Client LLC")
    session.add(right)
    session.flush()
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    session.add(DocumentLink(document_id=document.id, client_id=right.id,
                             method="manual", confidence=1.0, candidates=[]))
    session.flush()
    assert agenda(session, agency_id=1)[0].client_name == "Right Client LLC"


def test_status_reflects_the_latest_event(session):
    document = _document(session)
    _linked(session, document)
    row = _dated(session, document, date(2026, 7, 1), "policy_expiration")
    confirm(session, row.id)
    assert agenda(session, agency_id=1)[0].status == "confirmed"
    dismiss(session, row.id)
    # Asked for explicitly: the default agenda hides dismissed dates, which is
    # what the next test pins.
    entries = agenda(session, agency_id=1, statuses=ALL_STATUSES)
    assert entries[0].status == "dismissed"


def test_dismissed_dates_are_excluded_by_default(session):
    document = _document(session)
    _linked(session, document)
    row = _dated(session, document, date(2026, 7, 1), "policy_expiration")
    dismiss(session, row.id)
    assert agenda(session, agency_id=1) == []


def test_manual_dates_appear_alongside_extracted_ones(session):
    document = _document(session)
    client = _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    session.add(ManualDate(agency_id=1, client_id=client.id,
                           title="Call about the audit",
                           date_value=date(2026, 6, 1), date_type="audit_date",
                           created_by="human"))
    session.flush()
    kinds = [e.kind for e in agenda(session, agency_id=1)]
    assert sorted(kinds) == ["document_date", "manual_date"]


def test_a_cancellation_date_is_escalated_and_pinned_first(session):
    """Regardless of date proximity. This is the interrupt-everything event."""
    document = _document(session)
    _linked(session, document)
    _dated(session, document, date(2026, 1, 1), "policy_expiration")
    _dated(session, document, date(2027, 12, 1), "cancellation_effective")
    entries = agenda(session, agency_id=1)
    assert entries[0].date_type == "cancellation_effective"
    assert entries[0].escalated is True


def test_a_misclassified_cancellation_notice_still_escalates(session):
    """Display must never depend on classification being right, so the
    date_type escalates on its own."""
    document = _document(session)
    _linked(session, document)
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="unknown", confidence=0.1,
                                       classifier_version="classify-v1",
                                       model_id="stub"))
    session.flush()
    _dated(session, document, date(2027, 12, 1), "cancellation_effective")
    assert agenda(session, agency_id=1)[0].escalated is True


def test_a_cancellation_notice_escalates_its_other_dates_too(session):
    document = _document(session)
    _linked(session, document)
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="cancellation_notice",
                                       confidence=0.9,
                                       classifier_version="classify-v1",
                                       model_id="stub"))
    session.flush()
    _dated(session, document, date(2027, 12, 1), "payment_due")
    assert agenda(session, agency_id=1)[0].escalated is True


def test_non_escalated_entries_are_ordered_by_date(session):
    document = _document(session)
    _linked(session, document)
    _dated(session, document, date(2026, 9, 1), "policy_expiration")
    _dated(session, document, date(2026, 7, 1), "renewal_due")
    values = [e.date_value for e in agenda(session, agency_id=1)]
    assert values == sorted(values)


def test_filters_by_client_and_type_and_range(session):
    document = _document(session)
    client = _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    _dated(session, document, date(2026, 9, 1), "audit_date")
    assert len(agenda(session, agency_id=1, client_id=client.id)) == 2
    assert len(agenda(session, agency_id=1, date_types=("audit_date",))) == 1
    assert len(agenda(session, agency_id=1, start=date(2026, 8, 1))) == 1


def test_derived_dates_carry_their_arithmetic_into_the_entry(session):
    document = _document(session)
    _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "cancellation_effective",
           is_derived=True, anchor_date=date(2026, 6, 1),
           anchor_source_text="Dated: June 1, 2026")
    entry = agenda(session, agency_id=1)[0]
    assert entry.is_derived is True
    assert entry.anchor_date == date(2026, 6, 1)


def test_an_unlinked_documents_dates_still_appear(session):
    """A date is useful even when we do not yet know whose it is."""
    document = _document(session)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    entry = agenda(session, agency_id=1)[0]
    assert entry.client_id is None
    assert entry.client_name is None


def test_another_agencys_dates_are_not_shown(session):
    """agency_id is a boundary, not a label."""
    from renewal.models import Agency, Document
    other = Agency(slug="other", display_name="Some Other Agency",
                   ics_token="token-for-the-other-agency")
    session.add(other)
    session.flush()
    document = Document(blob_sha256="b" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=other.id)
    session.add(document)
    session.flush()
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    assert agenda(session, agency_id=1) == []
    assert len(agenda(session, agency_id=other.id)) == 1
