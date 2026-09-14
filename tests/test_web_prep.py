"""The prep sheet as a page.

It is read on paper as often as on screen, and it is opened while the phone is
ringing — so it renders from stored rows and nothing on it is a control that
does work.
"""

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.comparison import ColumnSpec, build_matrix
from renewal.config import Settings
from renewal.materiality import load_rules
from renewal.models import Client, Coverage, Policy, PolicyTerm
from renewal.web import create_app
from tests.authhelp import sign_in


class Exploding:
    """The sheet must not call a model. If it does, this says so loudly."""

    def complete(self, **kwargs):
        raise AssertionError("the prep sheet must not call a model")


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
        model_client=Exploding(),
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def signed(app, engine):
    @contextmanager
    def _open():
        with TestClient(app) as test_client:
            sign_in(test_client, engine)
            yield test_client

    return _open


@pytest.fixture
def client_id(engine):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Ramirez Landscaping")
    sess.add(client)
    sess.commit()
    out = client.id
    sess.close()
    return out


def _renewing_policy(engine, client_id, *, compared=True):
    """A policy expiring inside the window, with two terms and optionally a
    comparison between them."""
    sess = sessionmaker(bind=engine)()
    expires = date.today() + timedelta(days=19)
    policy = Policy(
        client_id=client_id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="commercial_auto",
        state="OR",
    )
    sess.add(policy)
    sess.flush()

    terms = []
    for premium, deductible in (("3900.00", "500"), ("4210.00", "1000")):
        term = PolicyTerm(
            policy_id=policy.id,
            kind="bound",
            carrier_name="Progressive",
            effective_date=expires - timedelta(days=365),
            expiration_date=expires,
            total_premium=premium,
        )
        sess.add(term)
        sess.flush()
        sess.add(
            Coverage(
                policy_term_id=term.id,
                coverage_code="COMP",
                deductible_value=deductible,
                premium="400.00",
            )
        )
        terms.append(term)
    sess.flush()

    if compared:
        build_matrix(
            sess,
            columns=[
                ColumnSpec(terms[0].id, "baseline"),
                ColumnSpec(terms[1].id, "comparand"),
            ],
            rules=load_rules("config/materiality.yaml"),
        )
    sess.commit()
    sess.close()


def test_the_sheet_renders_the_renewal_and_its_numbers(signed, client_id, engine):
    _renewing_policy(engine, client_id)
    with signed() as client:
        page = client.get(f"/clients/{client_id}/prep").text

    assert "Ramirez Landscaping" in page
    assert "Renews in 19 days" in page
    assert "+310.00" in page
    assert "COMP" in page
    assert "not attributable from these documents" in page


def test_the_sheet_says_nothing_is_a_recommendation(signed, client_id, engine):
    _renewing_policy(engine, client_id)
    with signed() as client:
        page = client.get(f"/clients/{client_id}/prep").text
    assert "nothing here is a recommendation" in page


def test_a_policy_with_no_comparison_offers_the_picker(signed, client_id, engine):
    _renewing_policy(engine, client_id, compared=False)
    with signed() as client:
        page = client.get(f"/clients/{client_id}/prep").text
    assert "No comparison has been built" in page
    assert "/compare" in page


def test_an_unknown_client_is_a_404(signed, engine):
    with signed() as client:
        assert client.get("/clients/999999/prep").status_code == 404


def test_the_client_page_links_to_the_sheet(signed, client_id, engine):
    _renewing_policy(engine, client_id)
    with signed() as client:
        page = client.get(f"/clients/{client_id}").text
    assert f"/clients/{client_id}/prep" in page
