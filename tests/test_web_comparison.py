from contextlib import contextmanager
import json
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.background import InlineRunner
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
    Document,
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

# The policy number is what files these against the policy: an exact match is
# the only auto-link D8 allows. Without it both documents would sit in the
# inbox waiting to be told who they belong to.
PRIOR = [
    "PROGRESSIVE AUTO",
    "Policy Number: AU-4471",
    "Total Policy Premium $1,840.00",
    "Effective Date: 07/01/2025",
    "Expiration Date: 07/01/2026",
]
RENEWAL = [
    "PROGRESSIVE AUTO",
    "Policy Number: AU-4471",
    "Total Policy Premium $2,180.00",
    "Effective Date: 07/01/2026",
    "Expiration Date: 07/01/2027",
]

DATE_MODEL = "date-model-for-tests"


class ScriptedClient:
    """One client for every stage, told apart by the model each one asks for.

    The original dispatched on a bare call count, which worked when the only
    way in was a form that called the extractor exactly twice. The pipeline
    classifies and date-extracts as well, so a bare count sends the wrong
    answer to the wrong stage.

    Within the extraction model the count is still how prior and renewal are
    told apart — the content handed to the extractor is the PDF, not its text,
    so there is nothing in it to read a premium off. The two documents go in
    in a fixed order, which is what makes that sound.
    """

    def __init__(self):
        self.extractions = 0

    def complete(self, *, model, system, content):
        if model == "claude-haiku-4-5-20251001":
            return json.dumps({"doc_class": "declarations", "confidence": 0.95})
        if model == DATE_MODEL:
            return json.dumps({"dates": []})
        if model == "claude-sonnet-5":
            return "Your renewal premium is $340 higher than last term."
        self.extractions += 1
        return self._extraction(PRIOR if self.extractions == 1 else RENEWAL)

    @staticmethod
    def _extraction(page):
        """Every source_text is a line from the page verbatim.

        Validation checks the cited text against the cited page; a source that
        paraphrases — "Effective Date: 2025-07-01" against a page that reads
        07/01/2025 — comes back flagged, the extraction lands 'partial', and
        the promote gate correctly declines. The stub has to be as honest as a
        real extractor is expected to be.
        """
        premium = page[2].split("$")[1]
        effective = page[3].split(": ")[1]
        expiration = page[4].split(": ")[1]

        def iso(american):
            month, day, year = american.split("/")
            return f"{year}-{month}-{day}"

        return json.dumps(
            {
                "fields": [
                    {
                        "field_path": path,
                        "value": value,
                        "confidence": 0.95,
                        "source_page": 1,
                        "source_text": text,
                    }
                    for path, value, text in (
                        ("policy.total_premium", premium.replace(",", ""),
                         page[2]),
                        ("policy.policy_number", "AU-4471", page[1]),
                        ("policy.effective_date", iso(effective), page[3]),
                        ("policy.expiration_date", iso(expiration), page[4]),
                    )
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
        # Distinct from draft_model, which also defaults to claude-sonnet-5.
        # The stub tells the stages apart by model name and cannot do that if
        # two of them answer to the same one.
        date_model=DATE_MODEL,
    )
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=ScriptedClient(),
        session_factory=sessionmaker(bind=engine),
        runner=InlineRunner(),
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


def _compare(client, engine):
    """Two dec pages through the front door, and what builds itself from them.

    This used to post a prior and a renewal to a form that asked which policy
    they belonged to. Both files now go in the same way any document does, and
    the comparison exists by the time the second one has been read.
    """
    for name, page in (("prior.pdf", PRIOR), ("renewal.pdf", RENEWAL)):
        response = client.post(
            "/documents",
            files={"document": (name, make_text_pdf([page]), "application/pdf")},
            follow_redirects=False,
        )
        assert response.status_code == 303, (response.status_code, response.text[:200])

    sess = sessionmaker(bind=engine)()
    try:
        return sess.query(Comparison).one().id
    finally:
        sess.close()


def test_two_dec_pages_become_terms_a_comparison_and_a_draft(
    signed, policy_id, engine
):
    """What POST /runs/{id}/promote used to do, reached with no human."""
    with signed() as client:
        _compare(client, engine)

    sess = sessionmaker(bind=engine)()
    assert sess.query(PolicyTerm).count() == 2
    assert sess.query(Comparison).count() == 1
    assert sess.query(Draft).count() == 1
    assert sess.query(Difference).count() >= 1
    sess.close()


def test_comparison_page_shows_draft_beside_the_diff(signed, policy_id, engine):
    with signed() as client:
        comparison_id = _compare(client, engine)
        page = client.get(f"/comparisons/{comparison_id}")
    assert "$340 higher" in page.text
    assert "policy.total_premium" in page.text
    assert "1840.00" in page.text
    assert "2180.00" in page.text
    assert "not attributable" in page.text


def test_editing_the_draft_writes_a_new_row(signed, policy_id, engine):
    with signed() as client:
        comparison_id = _compare(client, engine)
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
        _compare(client, engine)
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


# The two promote-gate tests that stood here went with POST /runs/{id}/promote.
# What they claimed is claimed now where the gate actually lives:
# tests/test_pipeline_promote.py — a flagged field stops the stage, and an
# acknowledged one lets it through.


def _diff_head(page):
    """The diff table's header row.

    Anchored on table.diffs: the premium breakdown table has a <thead> too and
    it comes first in the document.
    """
    return re.search(
        r'<table class="diffs">.*?<thead>(.*?)</thead>', page, re.S
    ).group(1)


def _a_document(sess) -> int:
    """A document row for a legacy RenewalRun to point at.

    Its bytes do not matter: nothing reads these two documents. What matters
    is that the foreign keys resolve, so the pre-matrix row is shaped exactly
    as it was when the run flow wrote it.
    """
    document = Document(
        blob_sha256=f"legacy-{uuid4().hex}", original_filename="legacy.pdf",
        page_count=1, has_text_layer=True, doc_type="dec_page",
        source="manual_upload", agency_id=1,
    )
    sess.add(document)
    sess.flush()
    return document.id


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


def test_a_legacy_comparison_still_renders(signed, policy_id, engine):
    """A comparison written before the matrix has a renewal_run_id.

    It used to render a crumb linking back to that run. The run screen is
    gone, so the crumb is the client on every comparison now — but the row
    still has to render, which is the whole reason the column stayed.
    """
    prior_id, renewal_id = _two_terms(engine, policy_id)
    sess = sessionmaker(bind=engine)()
    # Written directly: RenewalRun is never written by the application any
    # more, and this is a row from before it stopped. It still needs the pair
    # of documents it held, so they are written here too.
    run = RenewalRun(
        policy_id=policy_id,
        prior_document_id=_a_document(sess),
        renewal_document_id=_a_document(sess),
    )
    sess.add(run)
    sess.flush()
    comparison = Comparison(
        renewal_run_id=run.id, prior_term_id=prior_id, renewal_term_id=renewal_id
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
    assert "back to Ramirez Landscaping" in page
    # The screen that number pointed at no longer exists; nothing may link to
    # it, least of all a row that still carries the id.
    assert "/runs/" not in page


def test_a_comparison_crumb_points_at_the_client(
    signed, policy_id, engine
):
    """Every comparison, whether or not it carries a run id.

    This used to check that a comparison with no run did not render a link to
    run #None. No comparison renders a run link now, so the claim is the
    simpler one underneath it: the way back is the client.
    """
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
    assert "/runs/" not in page
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


def test_a_legacy_comparison_still_shows_its_draft_and_its_breakdown(
    signed, policy_id, engine
):
    """The whole of what a pre-matrix comparison had, not just its rows. This
    is the guarantee the checkpoint in Task 4 was placed to protect."""
    from renewal.models import Draft

    prior_id, renewal_id = _two_terms(engine, policy_id)
    sess = sessionmaker(bind=engine)()
    # Written directly: RenewalRun is never written by the application any
    # more, and this is a row from before it stopped. It still needs the pair
    # of documents it held, so they are written here too.
    run = RenewalRun(
        policy_id=policy_id,
        prior_document_id=_a_document(sess),
        renewal_document_id=_a_document(sess),
    )
    sess.add(run)
    sess.flush()
    comparison = Comparison(
        renewal_run_id=run.id, prior_term_id=prior_id, renewal_term_id=renewal_id
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
    sess.add(
        Draft(
            comparison_id=comparison.id,
            generated_text="Your renewal premium is $310 higher than last term.",
        )
    )
    sess.commit()
    comparison_id = comparison.id
    sess.close()

    with signed() as client:
        page = client.get(f"/comparisons/{comparison_id}").text

    assert "back to Ramirez Landscaping" in page      # its crumb
    assert "$310 higher" in page                      # its draft
    assert "not attributable" in page                 # its premium breakdown
    assert "Prior" in _diff_head(page)                # its two named columns
