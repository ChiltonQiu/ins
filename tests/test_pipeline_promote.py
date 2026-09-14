"""Promotion as a pipeline stage.

The gate is two facts that are already recorded: the document is linked to a
policy, and the extraction has nothing flagged. Neither is inferred here, and
promote() itself is unchanged — it still raises rather than guessing.
"""

import pytest

from renewal.models import (
    Client,
    Document,
    DocumentClassification,
    DocumentLink,
    ExtractedField,
    Extraction,
    Policy,
    PolicyTerm,
)
from renewal.pipeline import run_promote_stage
from tests.test_dates_llm import _settings


@pytest.fixture
def settings():
    return _settings()


def _document(session, sha="a"):
    document = Document(
        blob_sha256=sha * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    return document


def _policy(session, name="Acme Landscaping"):
    client = Client(display_name=name)
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="PA-1",
        line_of_business="commercial_auto",
        state="OR",
    )
    session.add(policy)
    session.flush()
    return policy


def _link(session, document, *, client_id, policy_id):
    session.add(
        DocumentLink(
            document_id=document.id,
            client_id=client_id,
            policy_id=policy_id,
            method="auto",
            confidence=1.0,
        )
    )
    session.flush()


def _extraction(session, document, *, needs_review=False):
    extraction = Extraction(
        document_id=document.id,
        extractor_version="v1",
        model_id="claude-opus-5",
        status="ok",
    )
    session.add(extraction)
    session.flush()
    session.add(
        ExtractedField(
            extraction_id=extraction.id,
            field_path="policy.total_premium",
            value="3900.00",
            confidence=0.4 if needs_review else 0.96,
            source_page=1,
            source_text_span="Total Policy Premium $3,900.00",
            needs_review=needs_review,
        )
    )
    session.flush()
    return extraction


def _classify(session, document, doc_class):
    session.add(
        DocumentClassification(
            document_id=document.id,
            doc_class=doc_class,
            confidence=0.9,
            classifier_version="v1",
            model_id="claude-haiku-4-5-20251001",
        )
    )
    session.flush()


def test_a_clean_extraction_on_a_linked_policy_promotes(session, settings):
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, document)

    term = run_promote_stage(session, document, settings=settings)
    assert term is not None
    assert term.policy_id == policy.id
    assert term.kind == "bound"
    assert term.total_premium == "3900.00"


def test_a_flagged_field_waits_for_a_human(session, settings):
    """promote() would raise PromotionBlocked. The stage does not catch that
    and carry on — it declines to call it."""
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, document, needs_review=True)

    assert run_promote_stage(session, document, settings=settings) is None
    assert session.query(PolicyTerm).count() == 0


def test_a_document_linked_to_a_client_but_no_policy_does_not_promote(
    session, settings
):
    """PolicyTerm.policy_id is not nullable, and guessing which of a client's
    four policies a dec page belongs to is the misfiling D8 exists to
    prevent."""
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=None)
    _extraction(session, document)

    assert run_promote_stage(session, document, settings=settings) is None


def test_an_unlinked_document_does_not_promote(session, settings):
    document = _document(session)
    _extraction(session, document)
    assert run_promote_stage(session, document, settings=settings) is None


def test_a_document_with_no_extraction_does_not_promote(session, settings):
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=policy.id)
    assert run_promote_stage(session, document, settings=settings) is None


def test_a_quote_promotes_as_a_quoted_term(session, settings):
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, document)
    _classify(session, document, "quote")

    term = run_promote_stage(session, document, settings=settings)
    assert term.kind == "quoted"


def test_a_declarations_document_promotes_as_bound(session, settings):
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, document)
    _classify(session, document, "declarations")

    term = run_promote_stage(session, document, settings=settings)
    assert term.kind == "bound"


def test_promoting_twice_writes_one_term(session, settings):
    """Idempotent like every other stage: a document already promoted at this
    extraction is skipped, not promoted again."""
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, document)

    assert run_promote_stage(session, document, settings=settings) is not None
    assert run_promote_stage(session, document, settings=settings) is None
    assert session.query(PolicyTerm).count() == 1


def test_the_same_document_on_a_different_policy_still_promotes(session, settings):
    """The guard against a re-delivery is same bytes and same policy. Two
    clients can hold the same form; that is not a duplicate."""
    first = _policy(session, "Acme Landscaping")
    second = _policy(session, "Borden Freight")

    one = _document(session, sha="a")
    _link(session, one, client_id=first.client_id, policy_id=first.id)
    _extraction(session, one)
    assert run_promote_stage(session, one, settings=settings) is not None

    # Same bytes, different policy.
    two = _document(session, sha="a")
    _link(session, two, client_id=second.client_id, policy_id=second.id)
    _extraction(session, two)
    term = run_promote_stage(session, two, settings=settings)
    assert term is not None
    assert term.policy_id == second.id


def test_the_same_bytes_on_the_same_policy_promote_once(session, settings):
    """Content addressing deduplicates the blob but not the document, so a
    resent dec page arrives as a second document with its own extraction."""
    policy = _policy(session)

    one = _document(session, sha="b")
    _link(session, one, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, one)
    assert run_promote_stage(session, one, settings=settings) is not None

    two = _document(session, sha="b")
    _link(session, two, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, two)
    assert run_promote_stage(session, two, settings=settings) is None
    assert session.query(PolicyTerm).count() == 1
