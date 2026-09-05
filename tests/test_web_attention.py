from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import (
    AttentionEvent, AttentionItem, Client, DateEvent, Document,
    DocumentClassification, DocumentDate, DocumentLink,
)
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
def db(engine):
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _document(sess, digest="a"):
    document = Document(blob_sha256=digest * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    sess.add(document)
    sess.flush()
    return document


@pytest.fixture
def seeded_cancellation_item(engine, clean_db):
    sess = sessionmaker(bind=engine)()
    document = _document(sess)
    sess.add(DocumentClassification(
        document_id=document.id, doc_class="cancellation_notice",
        confidence=0.9, classifier_version="classify-v1", model_id="stub"))
    item = AttentionItem(document_id=document.id,
                         reason_code="cancellation_notice",
                         reason_text="Classified as a cancellation notice")
    sess.add(item)
    sess.commit()
    got = item.id
    sess.close()
    yield got


@pytest.fixture
def seeded_near_date(engine, clean_db):
    sess = sessionmaker(bind=engine)()
    document = _document(sess, digest="b")
    row = DocumentDate(document_id=document.id,
                       date_value=date.today() + timedelta(days=5),
                       date_type="policy_expiration", source_page=1,
                       source_text="x", confidence=0.5,
                       extractor_version="dates-regex-v1", pass_name="regex")
    sess.add(row)
    sess.commit()
    got = row.id
    sess.close()
    yield got


@pytest.fixture
def seeded_client_with_item(engine, clean_db):
    sess = sessionmaker(bind=engine)()
    document = _document(sess, digest="c")
    client = Client(display_name="Acme Landscaping LLC")
    sess.add(client)
    sess.flush()
    sess.add(DocumentLink(document_id=document.id, client_id=client.id,
                          method="manual", confidence=1.0, candidates=[]))
    sess.add(AttentionItem(document_id=document.id,
                           reason_code="cancellation_notice",
                           reason_text="Classified as a cancellation notice"))
    sess.commit()
    got = client.id
    sess.close()
    yield got


def test_the_queue_lists_open_items(client_app, seeded_cancellation_item):
    body = client_app.get("/attention").text
    assert "cancellation notice" in body.lower()


def test_marking_done_appends_an_event(client_app, seeded_cancellation_item, db):
    response = client_app.post(f"/attention/{seeded_cancellation_item}/done",
                               follow_redirects=False)
    assert response.status_code == 303
    assert db.query(AttentionEvent).filter_by(
        attention_item_id=seeded_cancellation_item, action="done").count() == 1


def test_a_resolved_item_leaves_the_queue(client_app, seeded_cancellation_item):
    client_app.post(f"/attention/{seeded_cancellation_item}/done",
                    follow_redirects=False)
    assert "cancellation notice" not in client_app.get("/attention").text.lower()


def test_dismissing_a_live_date_row_writes_a_date_event(
    client_app, seeded_near_date, db
):
    """The live rule has no attention_item to resolve, so dismissal acts on the
    date itself — which is the right action anyway."""
    client_app.post(f"/dates/{seeded_near_date}/dismiss", follow_redirects=False)
    assert db.query(DateEvent).filter_by(document_date_id=seeded_near_date,
                                         action="dismissed").count() == 1


def test_a_dismissed_live_date_leaves_the_queue(client_app, seeded_near_date):
    before = client_app.get("/attention").text
    assert "policy expiration" in before.lower()
    client_app.post(f"/dates/{seeded_near_date}/dismiss", follow_redirects=False)
    assert "policy expiration" not in client_app.get("/attention").text.lower()


def test_an_escalated_reason_is_visually_distinct(
    client_app, seeded_cancellation_item
):
    assert "escalated" in client_app.get("/attention").text


def test_an_empty_queue_says_so(client_app):
    assert "nothing needs attention" in client_app.get("/attention").text.lower()


def test_the_client_overview_shows_that_clients_items(
    client_app, seeded_client_with_item
):
    body = client_app.get(f"/clients/{seeded_client_with_item}").text
    assert "cancellation notice" in body.lower()


def test_dismissing_an_item_also_clears_it(client_app, seeded_cancellation_item, db):
    client_app.post(f"/attention/{seeded_cancellation_item}/dismiss",
                    follow_redirects=False)
    assert db.query(AttentionEvent).filter_by(
        attention_item_id=seeded_cancellation_item, action="dismissed").count() == 1
