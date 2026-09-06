from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.carriers import set_admitted
from renewal.config import Settings
from renewal.models import (
    Carrier, Client, Document, DocumentDate, DocumentLink, Policy,
    PolicyBillingType, PolicyTerm,
)
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


def _build(engine, tmp_path, *, expires, billing_human, billing_term):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Acme Landscaping LLC")
    sess.add(client)
    sess.flush()
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    sess.add(carrier)
    sess.flush()
    set_admitted(sess, carrier.id, "CA", "non_admitted")
    policy = Policy(client_id=client.id,
                    carrier_name="Scottsdale Insurance Company",
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto", state="CA")
    sess.add(policy)
    sess.flush()
    sess.add(PolicyTerm(policy_id=policy.id,
                        carrier_name="Scottsdale Insurance Company",
                        effective_date=date(2025, 7, 1),
                        expiration_date=expires, total_premium="4820.00",
                        billing_type=billing_term))
    sess.add(PolicyBillingType(policy_id=policy.id,
                               billing_type=billing_human, set_by="human"))
    document = ingest_document(
        sess, BlobStore(tmp_path / "blobs"),
        data=make_text_pdf([["Expiration Date: 07/01/2099"]]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
    )
    sess.add(DocumentLink(document_id=document.id, client_id=client.id,
                          policy_id=policy.id, method="manual",
                          confidence=1.0, candidates=[]))
    sess.commit()
    ids = (client.id, document.id)
    sess.close()
    return ids


@pytest.fixture
def seeded_client(engine, tmp_path):
    client_id, _ = _build(engine, tmp_path, expires=date(2099, 7, 1),
                          billing_human="direct_bill", billing_term="direct_bill")
    yield client_id


@pytest.fixture
def seeded_document(engine, tmp_path):
    _, document_id = _build(engine, tmp_path, expires=date(2099, 7, 1),
                            billing_human="direct_bill",
                            billing_term="direct_bill")
    yield document_id


@pytest.fixture
def seeded_mismatch(engine, tmp_path):
    client_id, _ = _build(engine, tmp_path, expires=date(2099, 7, 1),
                          billing_human="agency_bill", billing_term="direct_bill")
    yield client_id


@pytest.fixture
def seeded_renewal(engine, tmp_path):
    client_id, _ = _build(engine, tmp_path,
                          expires=date.today() + timedelta(days=30),
                          billing_human="direct_bill", billing_term="direct_bill")
    yield client_id


@pytest.fixture
def empty_client(engine, clean_db):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Brand New LLC")
    sess.add(client)
    sess.commit()
    got = client.id
    sess.close()
    yield got


def test_the_client_list_renders(client_app, seeded_client):
    assert "Acme Landscaping LLC" in client_app.get("/clients").text


def test_the_overview_shows_policies_with_admitted_and_billing(
    client_app, seeded_client
):
    body = client_app.get(f"/clients/{seeded_client}").text
    assert "CAP-7781-22" in body
    assert "non-admitted" in body.lower()
    assert "direct bill" in body.lower()


def test_a_billing_mismatch_is_visible(client_app, seeded_mismatch):
    assert "mismatch" in client_app.get(f"/clients/{seeded_mismatch}").text.lower()


def test_an_unconfirmed_upcoming_date_is_flagged(client_app, seeded_client):
    assert "unconfirmed" in client_app.get(f"/clients/{seeded_client}").text.lower()


def test_a_renewal_inside_sixty_days_shows_a_countdown(client_app, seeded_renewal):
    assert "days" in client_app.get(f"/clients/{seeded_renewal}").text.lower()


def test_documents_link_to_the_pdf(client_app, seeded_client):
    assert "/documents/" in client_app.get(f"/clients/{seeded_client}").text


def test_the_pdf_route_serves_the_blob(client_app, seeded_document):
    response = client_app.get(f"/documents/{seeded_document}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_a_missing_client_is_a_404_not_a_500(client_app):
    assert client_app.get("/clients/999999").status_code == 404


def test_a_client_with_nothing_renders(client_app, empty_client):
    assert client_app.get(f"/clients/{empty_client}").status_code == 200


def test_unknown_renders_as_the_word_not_as_blank(client_app, empty_client, engine):
    """Blank reads as 'nothing to worry about'. Unknown is not that."""
    sess = sessionmaker(bind=engine)()
    client = sess.get(Client, empty_client)
    policy = Policy(client_id=client.id, carrier_name="Never Heard Of Them",
                    policy_number="X-1", line_of_business="commercial_auto",
                    state=None)
    sess.add(policy)
    sess.commit()
    sess.close()
    body = client_app.get(f"/clients/{empty_client}").text
    assert "unknown" in body.lower()
