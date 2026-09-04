import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Agency
from renewal.web import create_app


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
    )
    app = create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def agency_token(engine):
    sess = sessionmaker(bind=engine)()
    token = sess.query(Agency).one().ics_token
    sess.close()
    yield token


def test_the_ics_feed_needs_the_token(client_app):
    assert client_app.get("/calendar/not-the-token.ics").status_code == 404


def test_the_ics_feed_serves_with_the_token(client_app, agency_token):
    response = client_app.get(f"/calendar/{agency_token}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")


def test_regenerating_the_token_kills_the_old_link(client_app, agency_token):
    client_app.post("/settings/regenerate-ics-token", follow_redirects=False)
    assert client_app.get(f"/calendar/{agency_token}.ics").status_code == 404


def test_the_settings_page_warns_that_the_link_is_a_credential(client_app):
    body = client_app.get("/settings").text
    assert "anyone with this link" in body.lower()
