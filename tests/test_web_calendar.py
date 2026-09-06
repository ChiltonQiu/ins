from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.dates.service import status_of
from renewal.models import DocumentDate, ManualDate
from renewal.pipeline import ingest_document
from renewal.web import create_app
from tests.authhelp import sign_in
from tests.pdfmaker import make_text_pdf


@pytest.fixture
def client_app(engine, clean_db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
        session_cookie_secure=False,
    )
    app = create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
    )
    with TestClient(app) as test_client:
        sign_in(test_client, engine)
        yield test_client


@pytest.fixture
def db(engine):
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _ingest(engine, tmp_path, lines):
    sess = sessionmaker(bind=engine)()
    document = ingest_document(
        sess, BlobStore(tmp_path / "blobs"), data=make_text_pdf([lines]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
    )
    sess.commit()
    document_id = document.id
    sess.close()
    return document_id


@pytest.fixture
def seeded_date(engine, tmp_path):
    """One document with one extracted policy_expiration date."""
    document_id = _ingest(engine, tmp_path, ["Expiration Date: 07/01/2026"])
    sess = sessionmaker(bind=engine)()
    row_id = sess.query(DocumentDate).filter_by(
        document_id=document_id, date_type="policy_expiration").one().id
    sess.close()
    yield row_id


@pytest.fixture
def seeded_cancellation(engine, tmp_path):
    """A far-off cancellation date and a nearer expiration, so ordering by
    proximity alone would put the expiration first."""
    document_id = _ingest(engine, tmp_path, ["Expiration Date: 01/01/2026"])
    sess = sessionmaker(bind=engine)()
    sess.add(DocumentDate(
        document_id=document_id, date_value=date(2027, 12, 1),
        date_type="cancellation_effective", source_page=1,
        source_text="Cancellation Effective: 12/01/2027", confidence=0.5,
        extractor_version="dates-regex-v1", pass_name="regex"))
    sess.commit()
    sess.close()
    yield document_id


@pytest.fixture
def seeded_derived(engine, tmp_path):
    document_id = _ingest(engine, tmp_path, ["Dated: June 1, 2026"])
    sess = sessionmaker(bind=engine)()
    sess.add(DocumentDate(
        document_id=document_id, date_value=date(2026, 7, 1),
        date_type="cancellation_effective", source_page=1,
        source_text="within 30 days of the date of this notice", confidence=0.7,
        extractor_version="dates-llm-v1", pass_name="llm", is_derived=True,
        anchor_date=date(2026, 6, 1), anchor_source_text="Dated: June 1, 2026"))
    sess.commit()
    sess.close()
    yield document_id


def test_agenda_is_the_default_view(client_app, seeded_date):
    response = client_app.get("/calendar")
    assert response.status_code == 200
    assert 'data-view="agenda"' in response.text


def test_an_unconfirmed_date_is_visually_marked(client_app, seeded_date):
    """Never render an unconfirmed extracted date as fact."""
    body = client_app.get("/calendar").text
    assert "unconfirmed" in body


def test_confirming_is_one_post(client_app, seeded_date, db):
    response = client_app.post(f"/dates/{seeded_date}/confirm",
                               follow_redirects=False)
    assert response.status_code == 303
    assert status_of(db, seeded_date) == "confirmed"


def test_dismissing_is_one_post(client_app, seeded_date, db):
    client_app.post(f"/dates/{seeded_date}/dismiss", follow_redirects=False)
    assert status_of(db, seeded_date) == "dismissed"


def test_a_cancellation_date_is_pinned_and_marked_escalated(
    client_app, seeded_cancellation
):
    body = client_app.get("/calendar").text
    assert "escalated" in body
    assert body.index("escalated") < body.index("policy expiration")


def test_a_derived_date_shows_its_arithmetic(client_app, seeded_derived):
    body = client_app.get("/calendar").text
    assert "computed" in body


def test_she_can_add_a_date_that_came_from_no_document(client_app, db):
    """The only way this replaces the handwritten list."""
    response = client_app.post("/manual-dates", data={
        "title": "Call about the audit", "date_value": "2026-09-01",
        "date_type": "audit_date"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.query(ManualDate).filter_by(title="Call about the audit").count() == 1


def test_filters_are_applied(client_app, seeded_date):
    assert client_app.get("/calendar?date_type=audit_date").status_code == 200


def test_month_view_is_reachable(client_app, seeded_date):
    assert client_app.get("/calendar/month").status_code == 200


def test_each_entry_links_to_its_source_document(client_app, seeded_date):
    assert "/documents/" in client_app.get("/calendar").text


def test_the_source_document_link_serves_the_pdf(client_app, seeded_date, db):
    """A link to a 404 is not a link to the source."""
    document_id = db.get(DocumentDate, seeded_date).document_id
    response = client_app.get(f"/documents/{document_id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_a_dismissed_date_leaves_the_agenda(client_app, seeded_date):
    client_app.post(f"/dates/{seeded_date}/dismiss", follow_redirects=False)
    assert "policy expiration" not in client_app.get("/calendar").text


def test_a_foreign_referrer_is_refused_before_the_route(client_app, seeded_date):
    """The gate now stops a cross-site post outright, so this never reaches
    _back_to at all. The route keeps its own discarding anyway — see below —
    because the gate is one lock, not the only one."""
    response = client_app.post(
        f"/dates/{seeded_date}/confirm",
        headers={"referer": "https://evil.example/calendar?x=1"},
        follow_redirects=False,
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "referer,expected",
    [
        ("https://evil.example/calendar?x=1", "/calendar"),
        ("http://testserver/calendar/month?date_type=audit_date",
         "/calendar/month?date_type=audit_date"),
        ("http://testserver/settings", "/calendar"),
        ("", "/calendar"),
    ],
)
def test_only_a_local_calendar_referrer_survives(referer, expected):
    """Anything but the path of one of our own calendar URLs is discarded:
    otherwise any page linking here could choose where this app sends her
    next."""
    from starlette.requests import Request

    from renewal.web.calendar import _back_to

    scope = {
        "type": "http", "method": "POST", "path": "/dates/1/confirm",
        "headers": [(b"host", b"testserver")]
                   + ([(b"referer", referer.encode())] if referer else []),
        "query_string": b"", "scheme": "http", "server": ("testserver", 80),
    }
    assert _back_to(Request(scope)) == expected


def test_a_local_calendar_referrer_keeps_its_filters(client_app, seeded_date):
    response = client_app.post(
        f"/dates/{seeded_date}/confirm",
        headers={"referer": "http://testserver/calendar/month?date_type=audit_date"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/calendar/month?date_type=audit_date"


def test_a_hostile_filename_cannot_forge_a_header(client_app, db, engine, tmp_path):
    """The filename comes from whoever uploaded the document."""
    from renewal.models import Document

    sess = sessionmaker(bind=engine)()
    document = ingest_document(
        sess, BlobStore(tmp_path / "blobs"), data=make_text_pdf([["x"]]),
        original_filename='e"vil\r\nX-Injected: yes.pdf',
        source="bulk_import", agency_id=1,
    )
    sess.commit()
    document_id = document.id
    sess.close()

    response = client_app.get(f"/documents/{document_id}")
    assert response.status_code == 200
    assert "x-injected" not in {k.lower() for k in response.headers}
    disposition = response.headers["content-disposition"]
    assert '"' not in disposition.split("filename=", 1)[1].strip('"')
    assert "\n" not in disposition and "\r" not in disposition
