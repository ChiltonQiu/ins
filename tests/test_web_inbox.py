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
        page = client.get("/")
    assert page.status_code == 200
    assert "mystery.pdf" in page.text
    assert "who is this for" in page.text


def test_a_processing_document_shows_as_working_on_it(signed, engine, settings):
    _document(engine, settings, filename="inflight.pdf", status="processing")
    with signed() as client:
        page = client.get("/")
    assert "Reading this now" in page.text


def test_a_long_running_document_shows_as_stalled(signed, engine, settings):
    _document(engine, settings, filename="stuck.pdf", status="processing",
              age=timedelta(hours=3))
    with signed() as client:
        page = client.get("/")
    assert "restarted while this was in flight" in page.text


def test_an_empty_inbox_says_so_rather_than_showing_three_empty_headings(
    signed,
):
    with signed() as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "Nothing has come in yet" in page.text


def test_the_inbox_no_longer_lists_runs(signed):
    with signed() as client:
        page = client.get("/")
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
    assert response.headers["location"] == "/"

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
        page = client.get("/")
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
