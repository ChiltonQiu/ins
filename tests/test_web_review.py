from contextlib import contextmanager
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.config import Settings
from renewal.models import Client, Correction, Document, Extraction, Policy, RenewalRun
from renewal.web import create_app
from tests.authhelp import sign_in
from tests.pdfmaker import make_text_pdf

PRIOR = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $1,840.00",
]
RENEWAL = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $2,180.00",
]


class FakeClient:
    def __init__(self, response):
        self.response = response

    def complete(self, *, model, system, content):
        return self.response


def _response(premium, source):
    return json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": premium,
                    "confidence": 0.35,
                    "source_page": 1,
                    "source_text": source,
                }
            ]
        }
    )


@pytest.fixture
def app(engine, clean_db, tmp_path):
    from renewal.blobstore import BlobStore

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
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=FakeClient(_response("1840.00", "Total Policy Premium $1,840.00")),
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def signed(app, engine):
    """A signed-in TestClient, as a context manager.

    These tests open a fresh client in several places within one test, and a
    fresh client carries no cookie, so signing in belongs at each open rather
    than once per test.
    """
    @contextmanager
    def _open():
        with TestClient(app) as test_client:
            sign_in(test_client, engine)
            yield test_client

    return _open


@pytest.fixture
def seeded(engine):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Ramirez Landscaping")
    sess.add(client)
    sess.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    sess.add(policy)
    sess.commit()
    ids = (client.id, policy.id)
    sess.close()
    yield ids


def _upload(client, policy_id):
    return client.post(
        "/runs",
        data={"policy_id": str(policy_id)},
        files={
            "prior": ("prior.pdf", make_text_pdf([PRIOR]), "application/pdf"),
            "renewal": ("renewal.pdf", make_text_pdf([RENEWAL]), "application/pdf"),
        },
        follow_redirects=False,
    )


def test_new_run_form_lists_policies(signed, seeded):
    with signed() as client:
        page = client.get("/runs/new")
    assert page.status_code == 200
    assert "Ramirez Landscaping" in page.text


def test_upload_ingests_both_documents_and_extracts_each(signed, seeded, engine):
    _, policy_id = seeded
    with signed() as client:
        response = _upload(client, policy_id)
    assert response.status_code == 303
    assert "/review" in response.headers["location"]

    sess = sessionmaker(bind=engine)()
    assert sess.query(RenewalRun).count() == 1
    assert sess.query(Document).count() == 2
    assert sess.query(Extraction).count() == 2
    sess.close()


def test_review_page_shows_value_confidence_and_source(signed, seeded):
    _, policy_id = seeded
    with signed() as client:
        location = _upload(client, policy_id).headers["location"]
        page = client.get(location)
    assert "policy.total_premium" in page.text
    assert "1840.00" in page.text
    assert "Total Policy Premium $1,840.00" in page.text
    assert "page 1" in page.text
    assert "needs review" in page.text  # confidence 0.35 < 0.80


def test_correcting_a_field_writes_a_correction_and_leaves_the_field(signed, seeded, engine
):
    _, policy_id = seeded
    with signed() as client:
        location = _upload(client, policy_id).headers["location"]
        sess = sessionmaker(bind=engine)()
        field_id = sess.query(Extraction).first().fields[0].id
        sess.close()

        response = client.post(
            f"/fields/{field_id}/correct", data={"corrected_value": "1804.00"}
        )
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    correction = sess.query(Correction).one()
    assert correction.kind == "wrong_value"
    assert correction.extracted_value == "1840.00"
    assert correction.corrected_value == "1804.00"
    assert correction.extracted_field_id == field_id
    sess.close()


def test_adding_a_missing_field_records_an_omission(signed, seeded, engine):
    _, policy_id = seeded
    with signed() as client:
        _upload(client, policy_id)
        sess = sessionmaker(bind=engine)()
        extraction_id = sess.query(Extraction).first().id
        sess.close()

        response = client.post(
            f"/extractions/{extraction_id}/fields",
            data={
                "field_path": "coverage.UMBI.limit_value",
                "corrected_value": "100/300",
            },
        )
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    correction = sess.query(Correction).one()
    assert correction.kind == "omission"
    assert correction.extracted_field_id is None
    sess.close()


def test_rejecting_a_field_records_a_hallucination(signed, seeded, engine):
    _, policy_id = seeded
    with signed() as client:
        _upload(client, policy_id)
        sess = sessionmaker(bind=engine)()
        field_id = sess.query(Extraction).first().fields[0].id
        sess.close()

        response = client.post(f"/fields/{field_id}/reject")
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    assert sess.query(Correction).one().kind == "hallucination"
    sess.close()


def test_identical_documents_in_both_slots_are_refused_without_confirmation(signed, seeded
):
    _, policy_id = seeded
    same = make_text_pdf([PRIOR])
    with signed() as client:
        response = client.post(
            "/runs",
            data={"policy_id": str(policy_id)},
            files={
                "prior": ("a.pdf", same, "application/pdf"),
                "renewal": ("b.pdf", same, "application/pdf"),
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "same document" in response.text


def test_session_is_closed_when_a_handler_raises(signed, engine):
    """A route that 404s mid-request must still return its connection to the
    pool. If a handler leaks its session on the exception path, the
    connection stays checked out after the response comes back."""
    with signed() as client:
        baseline = engine.pool.checkedout()
        response = client.post(
            "/fields/999999/correct", data={"corrected_value": "x"}
        )
        assert response.status_code == 404
        assert engine.pool.checkedout() == baseline


def test_review_screen_shows_the_verification_rate(signed, seeded):
    """That file's stub returns the same response for both documents, citing
    the prior page's premium. So the prior side verifies and the renewal side
    cannot: one screen shows both ends of the scale."""
    _, policy_id = seeded
    with signed() as client:
        location = _upload(client, policy_id).headers["location"]
        page = client.get(location)
    assert "100.0% verified" in page.text  # prior: the quote is on the page
    assert "0.0% verified" in page.text  # renewal: the quote is not
