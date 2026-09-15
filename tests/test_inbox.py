"""What became of a document, computed from what is recorded.

Nothing here writes. Every bucket is derived on read, so the state changes the
moment she acts rather than the next time something remembers to update a row.
"""

import json
from datetime import date, datetime, timedelta, timezone

from renewal.classify.runner import latest_class
from renewal.inbox import state_of
from renewal.ingest import ingest_pdf
from renewal.models import (
    Client, DocumentClassification, ExtractedField, Extraction, Policy,
    PolicyTerm,
)
from renewal.pipeline import ingest_document
from renewal.resolve.service import assign
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings

DEC_PAGE = [
    "PROGRESSIVE COMMERCIAL AUTO",
    "Policy Number: AU-4471",
    "Total Policy Premium $4,210.00",
    "Effective Date: 07/01/2026",
    "Expiration Date: 07/01/2027",
]


class Carrier:
    """One client for three stages, told apart by the model each one asks for."""

    def complete(self, *, model, system, content):
        settings = _settings()
        if model == settings.classification_model:
            return json.dumps({"doc_class": "declarations", "confidence": 0.95})
        if model == settings.date_model:
            return json.dumps({"dates": []})
        return json.dumps(
            {
                "fields": [
                    {
                        "field_path": path,
                        "value": value,
                        "confidence": 0.97,
                        "source_page": 1,
                        "source_text": source,
                    }
                    for path, value, source in (
                        ("policy.total_premium", "4210.00",
                         "Total Policy Premium $4,210.00"),
                        ("policy.policy_number", "AU-4471",
                         "Policy Number: AU-4471"),
                        ("policy.effective_date", "2026-07-01",
                         "Effective Date: 07/01/2026"),
                        ("policy.expiration_date", "2027-07-01",
                         "Expiration Date: 07/01/2027"),
                    )
                ]
            }
        )


def _incumbent(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="commercial_auto",
        state="OR",
    )
    session.add(policy)
    session.flush()
    prior = PolicyTerm(
        policy_id=policy.id, kind="bound", carrier_name="Progressive",
        policy_number="AU-4471", effective_date=date(2025, 7, 1),
        expiration_date=date(2026, 7, 1), total_premium="3900.00",
    )
    session.add(prior)
    session.flush()
    return client, policy, prior


def _bare(session, store, filename="a.pdf"):
    return ingest_pdf(
        session, store, data=make_text_pdf([["nothing recognisable here"]]),
        original_filename=filename, source="manual_upload", agency_id=1,
    )


def test_processing_is_working_on_it(session, store):
    document = _bare(session, store)
    document.status = "processing"
    document.status_changed_at = datetime.now(timezone.utc)
    session.flush()
    assert state_of(session, document).bucket == "working"


def test_processing_for_too_long_is_stalled(session, store):
    """The one failure mode in-process background work has, made visible."""
    document = _bare(session, store)
    document.status = "processing"
    document.status_changed_at = datetime.now(timezone.utc) - timedelta(hours=3)
    session.flush()
    assert state_of(session, document).bucket == "stalled"


def test_a_failed_run_needs_her(session, store):
    document = _bare(session, store)
    document.status = "failed"
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "failed")


def test_no_link_at_all_needs_a_client(session, store):
    """Today's /unmatched, moved."""
    document = _bare(session, store)
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "needs_client")


def test_a_client_but_no_policy_needs_a_policy(session, store):
    client, _, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    # Overwrite the auto link with a client-only one: matched to a client,
    # no policy number it could attach to.
    assign(session, document.id, client_id=client.id, policy_id=None,
           candidates=[])
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "needs_policy")
    assert state.client_id == client.id


def test_a_document_that_does_not_route_is_done(session, store):
    """An invoice is stored and searchable. No term was ever expected, and
    saying 'needs you' about it would be a lie."""
    client, _, _ = _incumbent(session)
    document = _bare(session, store)
    assign(session, document.id, client_id=client.id, policy_id=None,
           candidates=[])
    session.add(
        DocumentClassification(
            document_id=document.id, doc_class="invoice", confidence=0.9,
            classifier_version="v1", model_id="test",
        )
    )
    session.flush()
    state = state_of(session, document)
    assert state.bucket == "done"
    assert "no policy term expected" in state.summary


def test_routed_but_never_extracted_is_not_stuck(session, store):
    """The bulk-import default, and the trap the stuck-documents spec named.

    Extraction was switched off deliberately for this document. It is
    not-yet-processed, not stuck, and putting it in 'needs you' would fill the
    queue with an entire archive nobody asked about.
    """
    client, policy, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
        extract_fields=False, model_client=Carrier(), settings=_settings(),
    )
    assert latest_class(session, document.id) == "declarations"
    assert session.query(Extraction).filter_by(
        document_id=document.id
    ).count() == 0
    assert state_of(session, document).bucket == "done"


def test_a_flagged_field_needs_review(session, store):
    client, policy, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    extraction = session.query(Extraction).filter_by(
        document_id=document.id
    ).one()
    field = session.query(ExtractedField).filter_by(
        extraction_id=extraction.id, field_path="policy.total_premium"
    ).one()
    field.needs_review = True
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "needs_review")
    assert state.extraction_id == extraction.id


def test_an_extraction_that_produced_nothing_says_so(session, store):
    client, policy, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    extraction = session.query(Extraction).filter_by(
        document_id=document.id
    ).one()
    session.query(ExtractedField).filter_by(
        extraction_id=extraction.id
    ).delete()
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "nothing_extracted")


def test_a_promoted_term_is_done_and_says_what_it_became(session, store):
    client, policy, prior = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="email_attachment", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    term = session.query(PolicyTerm).filter_by(
        source_document_id=document.id
    ).one()
    state = state_of(session, document)
    assert state.bucket == "done"
    assert "AU-4471" in state.summary
    assert state.policy_id == policy.id
    assert term.kind == "bound"


def test_rows_come_back_newest_first(session, store):
    first = _bare(session, store, "first.pdf")
    second = _bare(session, store, "second.pdf")
    from renewal.inbox import inbox_rows

    rows = inbox_rows(session)
    assert [r.document.id for r in rows] == [second.id, first.id]


def test_buckets_put_stalled_at_the_top_of_working(session, store):
    from renewal.inbox import bucketed, inbox_rows

    fresh = _bare(session, store, "fresh.pdf")
    fresh.status = "processing"
    fresh.status_changed_at = datetime.now(timezone.utc)
    old = _bare(session, store, "old.pdf")
    old.status = "processing"
    old.status_changed_at = datetime.now(timezone.utc) - timedelta(hours=3)
    session.flush()

    groups = bucketed(inbox_rows(session))
    assert [s.document.id for s in groups["working"]] == [old.id, fresh.id]
    assert groups["working"][0].bucket == "stalled"


def test_the_needs_you_count_is_what_the_page_shows(session, store):
    from renewal.inbox import bucketed, inbox_rows, needs_you_count

    _bare(session, store, "a.pdf")
    _bare(session, store, "b.pdf")
    session.flush()

    assert needs_you_count(session) == 2
    assert len(bucketed(inbox_rows(session))["needs_you"]) == 2


def test_nothing_in_flight_when_nothing_is_processing(session, store):
    from renewal.inbox import anything_in_flight

    document = _bare(session, store)
    session.flush()
    assert not anything_in_flight(session)

    document.status = "processing"
    session.flush()
    assert anything_in_flight(session)
