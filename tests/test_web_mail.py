from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Agency, Document, InboundMessage
from renewal.web import create_app
from tests.pdfmaker import make_text_pdf


class Refusing:
    """A provider that authenticates nothing successfully."""

    def verify(self, headers, body):
        return False

    def parse(self, headers, body):
        raise AssertionError("parse must not run when verification fails")


def _settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )


def _app(engine, tmp_path, provider=None):
    settings = _settings(tmp_path)
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
        inbound_provider=provider,
    )


@pytest.fixture
def client_app(engine, clean_db, tmp_path):
    with TestClient(_app(engine, tmp_path)) as test_client:
        yield test_client


@pytest.fixture
def client_app_unverified(engine, clean_db, tmp_path):
    with TestClient(_app(engine, tmp_path, provider=Refusing())) as test_client:
        yield test_client


@pytest.fixture
def db(engine):
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture
def agency_intake(engine):
    sess = sessionmaker(bind=engine)()
    agency = sess.query(Agency).filter_by(slug="default").one()
    agency.intake_address = "intake+default@example.com"
    sess.commit()
    sess.close()
    yield "intake+default@example.com"


def _eml(to="intake+default@example.com"):
    message = EmailMessage()
    message["Message-ID"] = "<abc123@carrier.example>"
    message["From"] = "underwriting@carrier.example"
    message["To"] = to
    message["Subject"] = "Notice of cancellation"
    message["Date"] = "Mon, 01 Jun 2026 09:00:00 -0700"
    message.set_content("Cancellation effective 07/01/2026.")
    message.add_attachment(make_text_pdf([["NOTICE OF CANCELLATION"]]),
                           maintype="application", subtype="pdf",
                           filename="notice.pdf")
    return message.as_bytes()


@pytest.fixture
def eml():
    yield _eml()


@pytest.fixture
def eml_to_stranger():
    yield _eml(to="intake+nobody@example.com")


def test_a_valid_message_returns_200_and_is_stored(client_app, agency_intake, eml):
    response = client_app.post("/inbound/mail", content=eml)
    assert response.status_code == 200


def test_the_attachments_and_body_become_documents(client_app, agency_intake, eml, db):
    client_app.post("/inbound/mail", content=eml)
    sources = {d.source for d in db.query(Document).all()}
    assert sources == {"email_body", "email_attachment"}


def test_an_unknown_recipient_still_returns_200_but_quarantines(
    client_app, agency_intake, eml_to_stranger, db
):
    """A non-200 makes the provider retry forever. Accept and quarantine."""
    assert client_app.post("/inbound/mail", content=eml_to_stranger).status_code == 200
    assert db.query(InboundMessage).one().processing_status == "quarantined"
    assert db.query(Document).count() == 0


def test_a_failed_verification_is_rejected_without_processing(
    client_app_unverified, eml, db
):
    assert client_app_unverified.post("/inbound/mail", content=eml).status_code == 403
    assert db.query(InboundMessage).count() == 0


def test_unparseable_mime_returns_200_and_records_the_failure(
    client_app, agency_intake, db
):
    client_app.post("/inbound/mail", content=b"this is not MIME at all")
    assert db.query(InboundMessage).one().processing_status in ("failed", "quarantined")


def test_a_duplicate_delivery_returns_200_and_adds_nothing(
    client_app, agency_intake, eml, db
):
    """A hosted provider retries. The second delivery must be cheap and inert."""
    client_app.post("/inbound/mail", content=eml)
    assert client_app.post("/inbound/mail", content=eml).status_code == 200
    assert db.query(InboundMessage).count() == 1
    assert db.query(Document).count() == 2
