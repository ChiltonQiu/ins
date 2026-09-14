from contextlib import contextmanager
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.carriers import set_admitted
from renewal.comparison import ColumnSpec, build_matrix
from renewal.config import Settings
from renewal.materiality import load_rules
from renewal.models import (
    Carrier,
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
        state="OR",
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


def _diff_head(page):
    """The diff table's header row.

    Anchored on table.diffs: the premium breakdown table has a <thead> too and
    it comes first in the document.
    """
    return re.search(
        r'<table class="diffs">.*?<thead>(.*?)</thead>', page, re.S
    ).group(1)


def _two_terms(engine, policy_id, *, prior="3900.00", renewal="4210.00"):
    """Two bound terms on one policy, written straight to the record.

    The run-and-promote path above is the other way in; these tests want a
    comparison whose run id they choose, including no run at all.
    """
    sess = sessionmaker(bind=engine)()
    terms = [
        PolicyTerm(
            policy_id=policy_id,
            kind="bound",
            carrier_name="Progressive",
            policy_number="AU-4471",
            total_premium=premium,
        )
        for premium in (prior, renewal)
    ]
    sess.add_all(terms)
    sess.commit()
    out = [term.id for term in terms]
    sess.close()
    return out


def test_a_two_column_comparison_still_renders_prior_and_renewal(
    signed, policy_id, engine
):
    """The checkpoint. Nothing a reader sees may move in this task."""
    prior_id, renewal_id = _two_terms(engine, policy_id)
    sess = sessionmaker(bind=engine)()
    comparison = build_matrix(
        sess,
        columns=[
            ColumnSpec(prior_id, "baseline"),
            ColumnSpec(renewal_id, "comparand"),
        ],
        rules=load_rules("config/materiality.yaml"),
    )
    sess.commit()
    comparison_id = comparison.id
    sess.close()

    with signed() as client:
        page = client.get(f"/comparisons/{comparison_id}").text
    assert "3900.00" in page and "4210.00" in page
    assert "premium_total_change" in page
    assert "not attributable" in page
    # A renewal's two columns are the same carrier, so they are still named
    # by term rather than by carrier.
    head = _diff_head(page)
    assert "Prior" in head and "Renewal" in head


def test_a_legacy_comparison_renders_and_links_back_to_its_run(
    signed, policy_id, engine
):
    """A comparison written before the matrix has a run; the crumb points at
    it. One written from the record does not, and must not render a link to
    run #None."""
    with signed() as client:
        run_id = _run(client, policy_id)

    prior_id, renewal_id = _two_terms(engine, policy_id)
    sess = sessionmaker(bind=engine)()
    comparison = Comparison(
        renewal_run_id=run_id, prior_term_id=prior_id, renewal_term_id=renewal_id
    )
    sess.add(comparison)
    sess.flush()
    sess.add(
        Difference(
            comparison_id=comparison.id,
            field_path="policy.total_premium",
            prior_value="3900.00",
            renewal_value="4210.00",
            materiality="material",
            rule_id="premium_total_change",
        )
    )
    sess.commit()
    comparison_id = comparison.id
    sess.close()

    with signed() as client:
        page = client.get(f"/comparisons/{comparison_id}").text
    assert "3900.00" in page and "4210.00" in page
    assert f"/runs/{run_id}/review" in page


def test_a_comparison_with_no_run_does_not_render_a_run_crumb(
    signed, policy_id, engine
):
    prior_id, renewal_id = _two_terms(engine, policy_id)
    sess = sessionmaker(bind=engine)()
    comparison = build_matrix(
        sess,
        columns=[
            ColumnSpec(prior_id, "baseline"),
            ColumnSpec(renewal_id, "comparand"),
        ],
        rules=load_rules("config/materiality.yaml"),
    )
    sess.commit()
    comparison_id = comparison.id
    sess.close()

    with signed() as client:
        page = client.get(f"/comparisons/{comparison_id}").text
    assert "/runs/None/review" not in page
    assert "back to Ramirez Landscaping" in page


def test_three_columns_render_as_three_carriers(signed, policy_id, engine):
    """Task 4's actual new capability: the screen is N columns, not two.

    Nothing here is ranked — the two quotes are rendered in the order they
    were added, and neither is marked.
    """
    sess = sessionmaker(bind=engine)()
    incumbent = PolicyTerm(
        policy_id=policy_id,
        kind="bound",
        carrier_name="Progressive",
        policy_number="AU-4471",
        total_premium="3900.00",
    )
    quotes = [
        PolicyTerm(
            policy_id=policy_id,
            kind="quoted",
            carrier_name=carrier,
            policy_number="AU-4471",
            total_premium=premium,
        )
        for carrier, premium in (("Sentry", "4210.00"), ("Cincinnati", "3610.00"))
    ]
    sess.add_all([incumbent, *quotes])
    sess.flush()
    comparison = build_matrix(
        sess,
        columns=[
            ColumnSpec(incumbent.id, "baseline"),
            *[ColumnSpec(quote.id, "comparand") for quote in quotes],
        ],
        rules=load_rules("config/materiality.yaml"),
    )
    sess.commit()
    comparison_id = comparison.id
    sess.close()

    with signed() as client:
        page = client.get(f"/comparisons/{comparison_id}").text

    # Three value headers, by carrier, the incumbent's marked as the baseline.
    # Scoped to the diff table: "Renewal" is in the site brand on every page.
    head = _diff_head(page)
    assert "Progressive" in head and "Sentry" in head and "Cincinnati" in head
    assert head.count("<th") == 5  # Field, three carriers, Class
    assert "baseline" in head
    assert "Prior" not in head and "Renewal" not in head

    # Both deltas are shown; neither quote is attributed line by line.
    assert "+310.00" in page and "-290.00" in page
    assert "line-item attribution is not offered against a quote" in page

    # And no draft is offered, because setting carriers side by side is advice.
    assert "the call is yours" in page

    # Admitted status rides in every header, with no toggle. These carriers
    # are unresolved, so it says so in words rather than going blank.
    assert head.count("admitted-unknown") == 3
    assert head.count(">quote<") == 2


def test_a_non_admitted_carrier_says_so_in_the_header(signed, policy_id, engine):
    """Surplus lines is a fact about the paper she is holding, not a detail to
    go looking for."""
    sess = sessionmaker(bind=engine)()
    incumbent = PolicyTerm(
        policy_id=policy_id,
        kind="bound",
        carrier_name="Progressive",
        policy_number="AU-4471",
        total_premium="3900.00",
    )
    quote = PolicyTerm(
        policy_id=policy_id,
        kind="quoted",
        carrier_name="Scottsdale Insurance Company",
        policy_number="AU-4471",
        total_premium="3610.00",
    )
    sess.add_all([incumbent, quote])
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    sess.add(carrier)
    sess.flush()
    set_admitted(sess, carrier.id, "OR", "non_admitted")
    comparison = build_matrix(
        sess,
        columns=[
            ColumnSpec(incumbent.id, "baseline"),
            ColumnSpec(quote.id, "comparand"),
        ],
        rules=load_rules("config/materiality.yaml"),
    )
    sess.commit()
    comparison_id = comparison.id
    sess.close()

    with signed() as client:
        page = client.get(f"/comparisons/{comparison_id}").text
    assert "non-admitted" in _diff_head(page)
