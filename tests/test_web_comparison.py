from contextlib import contextmanager
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import (
    Client,
    Comparison,
    Difference,
    Draft,
    Extraction,
    Policy,
    PolicyTerm,
    Reclassification,
    RenewalRun,
)
from renewal.web import create_app
from tests.authhelp import sign_in
from tests.pdfmaker import make_text_pdf

PRIOR = ["PROGRESSIVE AUTO", "Total Policy Premium $1,840.00"]
RENEWAL = ["PROGRESSIVE AUTO", "Total Policy Premium $2,180.00"]


class ScriptedClient:
    """Returns extraction JSON for the first two calls, draft text after."""

    def __init__(self):
        self.calls = 0

    def complete(self, *, model, system, content):
        self.calls += 1
        if self.calls == 1:
            return self._extraction("1840.00", "Total Policy Premium $1,840.00")
        if self.calls == 2:
            return self._extraction("2180.00", "Total Policy Premium $2,180.00")
        return "Your renewal premium is $340 higher than last term."

    @staticmethod
    def _extraction(premium, source):
        return json.dumps(
            {
                "fields": [
                    {
                        "field_path": "policy.total_premium",
                        "value": premium,
                        "confidence": 0.95,
                        "source_page": 1,
                        "source_text": source,
                    }
                ]
            }
        )


@pytest.fixture
def app(engine, clean_db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config="config/materiality.yaml",
        session_cookie_secure=False,
    )
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=ScriptedClient(),
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
def policy_id(engine):
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
    out = policy.id
    sess.close()
    yield out


def _run(client, policy_id):
    response = client.post(
        "/runs",
        data={"policy_id": str(policy_id)},
        files={
            "prior": ("prior.pdf", make_text_pdf([PRIOR]), "application/pdf"),
            "renewal": ("renewal.pdf", make_text_pdf([RENEWAL]), "application/pdf"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, (response.status_code, response.text[:200])
    return int(response.headers["location"].split("/")[2])


def test_promote_builds_terms_comparison_and_draft(signed, policy_id, engine):
    with signed() as client:
        run_id = _run(client, policy_id)
        response = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "/comparisons/" in response.headers["location"]

    sess = sessionmaker(bind=engine)()
    assert sess.query(PolicyTerm).count() == 2
    assert sess.query(Comparison).count() == 1
    assert sess.query(Draft).count() == 1
    assert sess.query(Difference).count() >= 1
    sess.close()


def test_comparison_page_shows_draft_beside_the_diff(signed, policy_id):
    with signed() as client:
        run_id = _run(client, policy_id)
        location = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        ).headers["location"]
        page = client.get(location)
    assert "$340 higher" in page.text
    assert "policy.total_premium" in page.text
    assert "1840.00" in page.text
    assert "2180.00" in page.text
    assert "not attributable" in page.text


def test_editing_the_draft_writes_a_new_row(signed, policy_id, engine):
    with signed() as client:
        run_id = _run(client, policy_id)
        location = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        ).headers["location"]
        comparison_id = int(location.split("/")[-1])
        client.post(
            f"/comparisons/{comparison_id}/draft",
            data={"final_text": "Edited by the agent."},
            follow_redirects=False,
        )

    sess = sessionmaker(bind=engine)()
    drafts = sess.query(Draft).order_by(Draft.id).all()
    assert len(drafts) == 2
    assert drafts[0].final_text is None
    assert drafts[1].final_text == "Edited by the agent."
    sess.close()


def test_reclassifying_from_the_ui_is_logged(signed, policy_id, engine):
    with signed() as client:
        run_id = _run(client, policy_id)
        location = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        ).headers["location"]
        sess = sessionmaker(bind=engine)()
        difference_id = sess.query(Difference).first().id
        sess.close()
        response = client.post(
            f"/differences/{difference_id}/reclassify",
            data={"to_materiality": "noise"},
        )
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    assert sess.query(Reclassification).count() == 1
    sess.close()


def test_promote_is_refused_while_a_field_needs_review(signed, policy_id, engine):
    with signed() as client:
        run_id = _run(client, policy_id)
        sess = sessionmaker(bind=engine)()
        for extraction in sess.query(Extraction).all():
            for field in extraction.fields:
                field.needs_review = True
        sess.commit()
        sess.close()

        response = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        )
    assert response.status_code == 400
    assert "policy.total_premium" in response.text


def test_acknowledging_a_field_allows_promotion(signed, policy_id, engine):
    with signed() as client:
        run_id = _run(client, policy_id)
        sess = sessionmaker(bind=engine)()
        for extraction in sess.query(Extraction).all():
            for field in extraction.fields:
                field.needs_review = True
        sess.commit()
        sess.close()

        response = client.post(
            f"/runs/{run_id}/promote",
            data={"acknowledged": "policy.total_premium"},
            follow_redirects=False,
        )
    assert response.status_code == 303
