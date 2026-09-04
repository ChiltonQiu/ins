import json
from datetime import date

from renewal.dates.service import confirm, dismiss, extract_dates, status_of
from renewal.models import DocumentDate
from renewal.pdftext import PageText
from tests.test_dates_llm import StubClient, _settings

PAGE = "Named Insured: Acme\nExpiration Date: 07/01/2026\nDated: June 1, 2026"


def _pages():
    return [PageText(1, PAGE)]


def _document(session, store):
    from renewal.pipeline import ingest_document
    from tests.pdfmaker import make_text_pdf
    return ingest_document(session, store, data=make_text_pdf([PAGE.splitlines()]),
                           original_filename="d.pdf", source="bulk_import",
                           agency_id=1)


def test_regex_dates_are_stored_with_their_pass_recorded(session, store):
    document = _document(session, store)
    stub = StubClient('{"dates": []}')
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    rows = session.query(DocumentDate).filter_by(document_id=document.id).all()
    assert rows
    assert {r.pass_name for r in rows} == {"regex"}


def test_a_hallucinated_source_text_is_rejected_not_stored(session, store):
    """The worst failure this system can produce is a confidently wrong date."""
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-09-09", "date_type": "cancellation_effective",
        "source_page": 1, "source_text": "Cancellation Effective: 09/09/2026",
        "confidence": 0.95}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    stored = session.query(DocumentDate).filter_by(
        document_id=document.id, pass_name="llm").all()
    assert stored == []


def test_a_source_page_out_of_range_is_rejected(session, store):
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "policy_expiration",
        "source_page": 99, "source_text": "Expiration Date: 07/01/2026",
        "confidence": 0.9}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    assert session.query(DocumentDate).filter_by(
        document_id=document.id, pass_name="llm").all() == []


def test_a_verified_llm_date_is_stored(session, store):
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "policy_expiration",
        "source_page": 1, "source_text": "Expiration Date: 07/01/2026",
        "confidence": 0.9}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    row = session.query(DocumentDate).filter_by(
        document_id=document.id, pass_name="llm").one()
    assert row.date_type == "policy_expiration"


def test_a_derived_date_with_a_verified_anchor_is_stored_with_it(session, store):
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "cancellation_effective",
        "source_page": 1, "source_text": "Dated: June 1, 2026",
        "confidence": 0.7, "is_derived": True, "anchor_date": "2026-06-01",
        "anchor_source_text": "Dated: June 1, 2026"}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    row = session.query(DocumentDate).filter_by(is_derived=True).one()
    assert row.anchor_date == date(2026, 6, 1)


def test_a_derived_date_with_an_unverifiable_anchor_is_kept_and_flagged(
    session, store
):
    """Stored so it reaches the calendar, flagged so it never reads as fact."""
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "cancellation_effective",
        "source_page": 1, "source_text": "Dated: June 1, 2026",
        "confidence": 0.7, "is_derived": True, "anchor_date": "2026-06-01",
        "anchor_source_text": "Dated: some other day entirely"}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    row = session.query(DocumentDate).filter_by(is_derived=True).one()
    assert row.anchor_date is None
    assert row.anchor_source_text is None


def test_an_unparseable_model_response_does_not_lose_the_regex_dates(session, store):
    document = _document(session, store)
    extract_dates(session, document, _pages(),
                  client=StubClient("I cannot read that."), settings=_settings())
    assert session.query(DocumentDate).filter_by(pass_name="regex").count() > 0


def test_a_date_is_unconfirmed_until_confirmed(session, store):
    document = _document(session, store)
    extract_dates(session, document, _pages(), client=StubClient('{"dates": []}'),
                  settings=_settings())
    row = session.query(DocumentDate).first()
    assert status_of(session, row.id) == "unconfirmed"
    confirm(session, row.id)
    assert status_of(session, row.id) == "confirmed"


def test_dismissal_after_confirmation_wins(session, store):
    """Latest event wins; she can change her mind."""
    document = _document(session, store)
    extract_dates(session, document, _pages(), client=StubClient('{"dates": []}'),
                  settings=_settings())
    row = session.query(DocumentDate).first()
    confirm(session, row.id)
    dismiss(session, row.id)
    assert status_of(session, row.id) == "dismissed"


def test_re_running_at_the_same_versions_does_not_duplicate(session, store):
    document = _document(session, store)
    stub = StubClient('{"dates": []}')
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    before = session.query(DocumentDate).count()
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    assert session.query(DocumentDate).count() == before
