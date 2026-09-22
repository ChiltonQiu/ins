"""The front door.

One screen that says what arrived, what happened to it, and what is left for
her. Every assertion here is about what she can see without knowing anything
about the pipeline underneath.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.ingest import ingest_pdf
from renewal.models import Client, Document, Policy
from renewal.resolve.service import assign
from renewal.web import create_app
from tests.authhelp import sign_in
from tests.pdfmaker import make_text_pdf


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
        session_cookie_secure=False,
    )


@pytest.fixture
def app(engine, clean_db, settings):
    from renewal.background import InlineRunner

    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
        runner=InlineRunner(),
    )


@pytest.fixture
def signed(app, engine):
    @contextmanager
    def _open():
        with TestClient(app) as test_client:
            sign_in(test_client, engine)
            yield test_client

    return _open


def _document(engine, settings, *, filename="a.pdf", status="processed",
              age=timedelta(0)):
    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["nothing recognisable here"]]),
            original_filename=filename, source="manual_upload", agency_id=1,
        )
        document.status = status
        document.status_changed_at = datetime.now(timezone.utc) - age
        session.commit()
        return document.id
    finally:
        session.close()


def test_the_inbox_lists_a_document_that_needs_a_client(
    signed, engine, settings
):
    _document(engine, settings, filename="mystery.pdf")
    with signed() as client:
        page = client.get("/inbox")
    assert page.status_code == 200
    assert "mystery.pdf" in page.text
    assert "who is this for" in page.text


def test_a_processing_document_shows_as_working_on_it(signed, engine, settings):
    _document(engine, settings, filename="inflight.pdf", status="processing")
    with signed() as client:
        page = client.get("/inbox")
    assert "Reading this now" in page.text


def test_a_long_running_document_shows_as_stalled(signed, engine, settings):
    _document(engine, settings, filename="stuck.pdf", status="processing",
              age=timedelta(hours=3))
    with signed() as client:
        page = client.get("/inbox")
    assert "restarted while this was in flight" in page.text


def test_an_empty_inbox_says_so_rather_than_showing_three_empty_headings(
    signed,
):
    with signed() as client:
        page = client.get("/inbox")
    assert page.status_code == 200
    assert "Nothing has come in yet" in page.text


def test_the_inbox_no_longer_lists_runs(signed):
    with signed() as client:
        page = client.get("/inbox")
    assert "Renewal runs" not in page.text


def test_dropping_a_file_in_files_it_and_answers_immediately(
    signed, engine, settings
):
    """The receipt. The row exists the instant the file lands, before any
    processing has happened — that is the confirmation that it arrived."""
    with signed() as client:
        response = client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/inbox"

    session = sessionmaker(bind=engine)()
    try:
        document = session.query(Document).one()
        assert document.original_filename == "dropped.pdf"
        assert document.source == "manual_upload"
    finally:
        session.close()


def test_the_dropped_file_appears_on_the_inbox(signed, engine, settings):
    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )
        page = client.get("/inbox")
    assert "dropped.pdf" in page.text


def test_the_stages_run_and_the_status_settles(signed, engine, settings):
    """With the inline runner the stages have finished by the time the
    response comes back, which is what lets this assert instead of sleep."""
    from renewal.models import DocumentText

    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )

    session = sessionmaker(bind=engine)()
    try:
        document = session.query(Document).one()
        assert document.status == "processed"
        assert session.query(DocumentText).filter_by(
            document_id=document.id
        ).count() == 1
    finally:
        session.close()


def test_a_file_that_is_not_a_pdf_is_refused_with_a_reason(signed):
    with signed() as client:
        response = client.post(
            "/documents",
            files={"document": ("notes.txt", b"just some text", "text/plain")},
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "PDF" in response.text


def test_retry_reruns_a_stalled_document(signed, engine, settings):
    from renewal.models import DocumentText

    document_id = _document(engine, settings, filename="stuck.pdf",
                            status="processing", age=timedelta(hours=3))
    with signed() as client:
        response = client.post(f"/documents/{document_id}/retry",
                               follow_redirects=False)
    assert response.status_code == 303

    session = sessionmaker(bind=engine)()
    try:
        assert session.get(Document, document_id).status == "processed"
        assert session.query(DocumentText).filter_by(
            document_id=document_id
        ).count() == 1
    finally:
        session.close()


def test_retrying_a_finished_document_changes_nothing(signed, engine, settings):
    """Idempotent by construction: every stage checks its own work first."""
    from renewal.models import DocumentText

    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )

    session = sessionmaker(bind=engine)()
    try:
        document_id = session.query(Document).one().id
    finally:
        session.close()

    with signed() as client:
        client.post(f"/documents/{document_id}/retry", follow_redirects=False)

    session = sessionmaker(bind=engine)()
    try:
        assert session.query(DocumentText).filter_by(
            document_id=document_id
        ).count() == 1
    finally:
        session.close()


def test_retrying_a_document_that_does_not_exist_is_a_404(signed):
    with signed() as client:
        response = client.post("/documents/999999/retry",
                               follow_redirects=False)
    assert response.status_code == 404


def test_a_stalled_row_offers_the_retry_button(signed, engine, settings):
    _document(engine, settings, filename="stuck.pdf", status="processing",
              age=timedelta(hours=3))
    with signed() as client:
        page = client.get("/inbox")
    assert "/retry" in page.text


def test_the_detail_view_shows_the_fields_and_the_source_link(
    signed, engine, settings
):
    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )

    session = sessionmaker(bind=engine)()
    try:
        document_id = session.query(Document).one().id
    finally:
        session.close()

    with signed() as client:
        page = client.get(f"/documents/{document_id}/review")
    assert page.status_code == 200
    assert "dropped.pdf" in page.text
    # Every claim in this app is one click from the page it was read off.
    assert f'href="/documents/{document_id}"' in page.text


def test_the_detail_view_of_a_missing_document_is_a_404(signed):
    with signed() as client:
        page = client.get("/documents/999999/review")
    assert page.status_code == 404


def test_a_needs_you_row_links_to_the_detail_view(signed, engine, settings):
    _document(engine, settings, filename="mystery.pdf")
    with signed() as client:
        page = client.get("/inbox")
    assert "/review" in page.text


def test_a_needs_client_row_offers_the_ranked_candidates(
    signed, engine, settings
):
    """A name that looks close is a candidate, not a decision.

    Deliberately not a policy number: an exact policy-number match scores 1.0
    and auto-links under D8, so such a document never reaches this bucket at
    all. The row that needs her is the one where the name is similar and
    nothing is certain.
    """
    session = sessionmaker(bind=engine)()
    try:
        session.add(Client(display_name="Ramirez Landscaping Incorporated"))
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["Named Insured: Ramirez Landscaping"]]),
            original_filename="mystery.pdf", source="manual_upload",
            agency_id=1,
        )
        session.commit()
        document_id = document.id
    finally:
        session.close()

    # Text has to exist for candidate extraction to read anything.
    with signed() as client:
        client.post(f"/documents/{document_id}/retry", follow_redirects=False)
        page = client.get("/inbox")

    assert f"/unmatched/{document_id}/assign" in page.text
    assert "Ramirez Landscaping Incorporated" in page.text


def test_assigning_a_client_from_the_inbox_lands_back_on_the_inbox(
    signed, engine, settings
):
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Ramirez Landscaping")
        session.add(client_row)
        session.flush()
        client_id = client_row.id
        session.commit()
    finally:
        session.close()

    document_id = _document(engine, settings, filename="mystery.pdf")
    with signed() as client:
        response = client.post(
            f"/unmatched/{document_id}/assign",
            data={"client_id": str(client_id)},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/inbox"


def test_the_old_unmatched_page_is_gone(signed):
    with signed() as client:
        page = client.get("/unmatched", follow_redirects=False)
    assert page.status_code == 404


def test_a_needs_policy_row_offers_that_clients_policies(
    signed, engine, settings
):
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Ramirez Landscaping")
        session.add(client_row)
        session.flush()
        policy = Policy(
            client_id=client_row.id, carrier_name="Progressive",
            policy_number="AU-4471", line_of_business="commercial_auto",
        )
        session.add(policy)
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["DECLARATIONS PAGE", "premium and limits"]]),
            original_filename="dec.pdf", source="manual_upload", agency_id=1,
        )
        session.flush()
        assign(session, document.id, client_id=client_row.id, policy_id=None,
               candidates=[])
        session.commit()
        document_id, policy_id = document.id, policy.id
    finally:
        session.close()

    with signed() as client:
        response = client.post(
            f"/documents/{document_id}/policy",
            data={"policy_id": str(policy_id)},
            follow_redirects=False,
        )
    assert response.status_code == 303

    session = sessionmaker(bind=engine)()
    try:
        from renewal.resolve.service import latest_link

        assert latest_link(session, document_id).policy_id == policy_id
    finally:
        session.close()


def test_the_nav_carries_the_needs_you_count(signed, engine, settings):
    """On every page, not just the inbox: the point of a badge is that she
    sees it while she is somewhere else."""
    _document(engine, settings, filename="a.pdf")
    _document(engine, settings, filename="b.pdf")
    with signed() as client:
        page = client.get("/calendar")
    assert 'class="badge">2<' in page.text


def test_an_empty_queue_shows_no_badge(signed):
    with signed() as client:
        page = client.get("/calendar")
    assert 'class="badge"' not in page.text


@pytest.mark.parametrize("path", ["/runs/new", "/runs/1/review"])
def test_the_run_pages_are_gone(signed, path):
    with signed() as client:
        page = client.get(path, follow_redirects=False)
    assert page.status_code == 404


@pytest.mark.parametrize("path", ["/runs", "/runs/1/promote"])
def test_the_run_posts_are_gone(signed, path):
    with signed() as client:
        page = client.post(path, follow_redirects=False)
    assert page.status_code in (404, 405)


def test_the_correction_endpoints_kept_their_urls(signed, engine, settings):
    """app.js posts to these by literal path. Moving them would break every
    correction silently, with a 404 nobody sees."""
    from renewal.models import Correction, Extraction

    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["Policy Number: AU-4471"]]),
            original_filename="dec.pdf", source="manual_upload", agency_id=1,
        )
        session.flush()
        extraction = Extraction(
            document_id=document.id, extractor_version="v1",
            model_id="claude-opus-5", status="ok",
        )
        session.add(extraction)
        session.commit()
        extraction_id = extraction.id
    finally:
        session.close()

    with signed() as client:
        response = client.post(
            f"/extractions/{extraction_id}/fields",
            data={"field_path": "policy.total_premium",
                  "corrected_value": "1234.00"},
        )
    assert response.status_code == 204

    session = sessionmaker(bind=engine)()
    try:
        assert session.query(Correction).filter_by(
            extraction_id=extraction_id
        ).count() == 1
    finally:
        session.close()
