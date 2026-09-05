import json
from datetime import date, timedelta

from renewal.attention.rules import (
    UNCONFIRMED_DATE_WINDOW_DAYS, evaluate, open_items, resolve,
)
from renewal.models import (
    AttentionItem, Client, Document, DocumentClassification, DocumentDate,
    DocumentLink,
)
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings

TODAY = date(2026, 6, 1)

# Several short lines, so the page clears MIN_CHARS_FOR_TEXT_LAYER and keeps
# its text layer instead of being routed to OCR.
def _lines(first):
    return [first,
            "Issued by the carrier for the policy named below.",
            "See the enclosed pages for the full terms of this notice."]


def _reasons(session, document_id):
    """Ingest already evaluated, so the invariant is the stored state rather
    than what a second evaluate() call returns."""
    return {row.reason_code for row in session.query(AttentionItem).filter_by(
        document_id=document_id)}


def _item(session, document_id, reason_code):
    return session.query(AttentionItem).filter_by(
        document_id=document_id, reason_code=reason_code).one()


def _linked_document(session, doc_class=None):
    """A document built directly, so evaluate() can be exercised on state that
    already exists. Going through the pipeline would evaluate it first, and
    these two tests are about what the rule does, not about ingest order."""
    document = Document(blob_sha256=f"{len(doc_class or ''):064x}",
                        original_filename="d.pdf", page_count=1,
                        has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="manual", confidence=1.0, candidates=[]))
    if doc_class:
        session.add(DocumentClassification(
            document_id=document.id, doc_class=doc_class, confidence=0.9,
            classifier_version="classify-v1", model_id="stub"))
    session.flush()
    return document


def _document(session, store, lines, doc_class="unknown"):
    from renewal.pipeline import ingest_document
    return ingest_document(
        session, store, data=make_text_pdf([lines]), original_filename="d.pdf",
        source="bulk_import", agency_id=1,
        model_client=StubClient(json.dumps({"doc_class": doc_class,
                                            "confidence": 0.9})),
        settings=_settings(),
    )


def test_a_cancellation_notice_is_flagged(session, store):
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    evaluate(session, document, settings=_settings())
    assert "cancellation_notice" in _reasons(session, document.id)


def test_a_non_renewal_notice_is_flagged(session, store):
    document = _document(session, store, _lines("NOTICE OF NON-RENEWAL"),
                         doc_class="non_renewal_notice")
    evaluate(session, document, settings=_settings())
    assert "non_renewal_notice" in _reasons(session, document.id)


def test_an_unmatched_document_is_flagged(session, store):
    document = _document(session, store, _lines("a page nobody can place"))
    evaluate(session, document, settings=_settings())
    assert "unmatched_document" in _reasons(session, document.id)


def test_a_matched_document_is_not_flagged_as_unmatched(session):
    document = _linked_document(session)
    evaluate(session, document, settings=_settings())
    assert "unmatched_document" not in _reasons(session, document.id)


def test_an_ordinary_invoice_is_not_flagged(session):
    """Over-flagging is the bias, not flagging everything."""
    document = _linked_document(session, doc_class="invoice")
    evaluate(session, document, settings=_settings())
    assert _reasons(session, document.id) == set()


def test_evaluation_is_idempotent(session, store):
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    evaluate(session, document, settings=_settings())
    evaluate(session, document, settings=_settings())
    assert session.query(AttentionItem).filter_by(
        document_id=document.id, reason_code="cancellation_notice").count() == 1


def test_a_near_unconfirmed_date_appears_in_the_queue_without_a_row(session, store):
    """It changes with the clock, so it is computed at read time rather than
    materialised — which would need a daemon we are not building."""
    document = _document(session, store, _lines("a page"))
    session.add(DocumentDate(
        document_id=document.id, date_value=TODAY + timedelta(days=7),
        date_type="cancellation_effective", source_page=1, source_text="x",
        confidence=0.5, extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    reasons = {r.reason_code for r in open_items(session, today=TODAY)}
    assert "unconfirmed_date_within_14_days" in reasons
    assert session.query(AttentionItem).filter_by(
        reason_code="unconfirmed_date_within_14_days").count() == 0


def test_a_date_beyond_the_window_is_not_in_the_queue(session, store):
    document = _document(session, store, _lines("a page"))
    session.add(DocumentDate(
        document_id=document.id,
        date_value=TODAY + timedelta(days=UNCONFIRMED_DATE_WINDOW_DAYS + 1),
        date_type="policy_expiration", source_page=1, source_text="x",
        confidence=0.5, extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    assert "unconfirmed_date_within_14_days" not in {
        r.reason_code for r in open_items(session, today=TODAY)}


def test_a_confirmed_date_leaves_the_queue(session, store):
    from renewal.dates.service import confirm
    document = _document(session, store, _lines("a page"))
    row = DocumentDate(document_id=document.id,
                       date_value=TODAY + timedelta(days=7),
                       date_type="policy_expiration", source_page=1,
                       source_text="x", confidence=0.5,
                       extractor_version="dates-regex-v1", pass_name="regex")
    session.add(row)
    session.flush()
    confirm(session, row.id)
    assert "unconfirmed_date_within_14_days" not in {
        r.reason_code for r in open_items(session, today=TODAY)}


def test_resolving_appends_an_event_and_clears_the_item(session, store):
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    item = _item(session, document.id, "cancellation_notice")
    resolve(session, item.id, action="done")
    assert item.id not in {r.item_id for r in open_items(session, today=TODAY)}


def test_nothing_resolves_itself(session, store):
    """We cannot detect that she replied without reading sent mail, which needs
    OAuth this project does not do."""
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    item = _item(session, document.id, "cancellation_notice")
    evaluate(session, document, settings=_settings())
    assert item.id in {r.item_id for r in open_items(session, today=TODAY)}


def test_a_resolved_item_is_never_revived_by_re_evaluation(session, store):
    """Her judgment outranks the rule that created the item."""
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    item = _item(session, document.id, "cancellation_notice")
    resolve(session, item.id, action="dismissed")
    evaluate(session, document, settings=_settings())
    assert item.id not in {r.item_id for r in open_items(session, today=TODAY)}
