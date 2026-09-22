"""Filing a document against a client.

The queue itself is now a bucket of the inbox (tests/test_web_inbox.py); what
is tested here is the pair of fixes that queue offers, and the record each one
writes. The record is the point: a manual link stores the candidates that were
on screen when she chose, which is the training data for making matching
better.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Client, DocumentLink
from renewal.pipeline import ingest_document
from renewal.web import create_app
from tests.authhelp import sign_in
from tests.pdfmaker import make_text_pdf

# The named insured matches a client that exists; the policy number matches
# nothing. That is the shape that has to queue rather than auto-link.
DEC = [
    "DECLARATIONS PAGE",
    "Named Insured: Acme Landscaping LLC",
    "Policy Number: NO-SUCH-9999",
]


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


@pytest.fixture
def seeded(engine, tmp_path):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Acme Landscaping LLC")
    sess.add(client)
    sess.flush()
    document = ingest_document(
        sess, BlobStore(tmp_path / "blobs"), data=make_text_pdf([DEC]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
    )
    sess.commit()
    ids = (document.id, client.id)
    sess.close()
    yield ids


def test_assigning_writes_a_manual_link_with_the_candidates_shown(
    client_app, seeded, db
):
    document_id, client_id = seeded
    response = client_app.post(
        f"/unmatched/{document_id}/assign",
        data={"client_id": str(client_id)}, follow_redirects=False,
    )
    assert response.status_code == 303
    link = db.query(DocumentLink).filter_by(document_id=document_id).one()
    assert link.method == "manual"
    assert link.client_id == client_id
    assert link.candidates != []


def test_creating_a_client_from_the_queue_links_the_document(client_app, seeded, db):
    document_id, _ = seeded
    client_app.post(f"/unmatched/{document_id}/new-client",
                    data={"display_name": "Brand New LLC"},
                    follow_redirects=False)
    created = db.query(Client).filter_by(display_name="Brand New LLC").one()
    link = db.query(DocumentLink).filter_by(document_id=document_id).one()
    assert link.client_id == created.id
    assert link.candidates == []


def test_an_assigned_document_leaves_the_queue(client_app, seeded):
    """The queue is a bucket of the inbox now, so this asks the inbox.

    Pointed at /unmatched it would pass on the 404 body — a test passing
    because the page it looks at does not exist.
    """
    document_id, client_id = seeded
    inbox = client_app.get("/inbox")
    assert f"/unmatched/{document_id}/assign" in inbox.text

    client_app.post(f"/unmatched/{document_id}/assign",
                    data={"client_id": str(client_id)}, follow_redirects=False)
    assert f"/unmatched/{document_id}/assign" not in client_app.get("/inbox").text


def test_assigning_a_client_that_does_not_exist_is_rejected(client_app, seeded):
    document_id, _ = seeded
    response = client_app.post(f"/unmatched/{document_id}/assign",
                               data={"client_id": "999999"},
                               follow_redirects=False)
    assert response.status_code == 404
