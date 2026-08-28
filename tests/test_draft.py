from decimal import Decimal

import pytest

from renewal.config import Settings
from renewal.draft import build_prompt, generate_draft, latest_draft, save_edit
from renewal.models import (
    Client,
    Comparison,
    Difference,
    Document,
    Draft,
    Policy,
    PolicyTerm,
    RenewalRun,
)
from renewal.premium import Attribution, PremiumBreakdown


class FakeClient:
    def __init__(self, text="Your premium went up by $340."):
        self.text = text
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append({"model": model, "system": system, "content": content})
        return self.text


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path,
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )


@pytest.fixture
def comparison(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    document = Document(
        blob_sha256="f" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    run = RenewalRun(
        policy_id=policy.id,
        prior_document_id=document.id,
        renewal_document_id=document.id,
    )
    session.add(run)
    session.flush()
    terms = []
    for premium in ("1840.00", "2180.00"):
        term = PolicyTerm(
            policy_id=policy.id, effective_date="2026-03-01", total_premium=premium
        )
        session.add(term)
        session.flush()
        terms.append(term)
    row = Comparison(
        renewal_run_id=run.id,
        prior_term_id=terms[0].id,
        renewal_term_id=terms[1].id,
    )
    session.add(row)
    session.flush()
    return row


def _differences():
    return [
        Difference(
            field_path="policy.total_premium",
            prior_value="1840.00",
            renewal_value="2180.00",
            materiality="material",
            rule_id="premium_total_change",
        ),
        Difference(
            field_path="forms.A085.edition_date",
            prior_value="2019-06",
            renewal_value="2024-01",
            materiality="noise",
            rule_id="form_edition",
        ),
    ]


def _breakdown(residual="222.00"):
    return PremiumBreakdown(
        available=True,
        total_delta=Decimal("340.00"),
        lines=[
            Attribution(
                label="COLL on VIN0001",
                field_path="item.VIN0001.coverage.COLL.premium",
                amount=Decimal("118.00"),
            )
        ],
        residual=Decimal(residual),
    )


def test_prompt_excludes_noise():
    prompt = build_prompt(_differences(), _breakdown())
    assert "policy.total_premium" in prompt
    assert "forms.A085" not in prompt


def test_prompt_states_the_residual_as_unexplained():
    prompt = build_prompt(_differences(), _breakdown())
    assert "222.00" in prompt
    assert "not attributable" in prompt


def test_prompt_says_so_when_attribution_is_unavailable():
    unavailable = PremiumBreakdown(
        available=False,
        total_delta=None,
        lines=[],
        residual=None,
        reason="total premium is not present on both documents",
    )
    assert "not present on both documents" in build_prompt(_differences(), unavailable)


def test_prompt_forbids_advice():
    prompt = build_prompt(_differences(), _breakdown())
    assert "Do not recommend" in prompt
    assert "200 words" in prompt


def test_generate_draft_persists_generated_text(session, settings, comparison):
    client = FakeClient()

    draft = generate_draft(
        session,
        comparison,
        _differences(),
        _breakdown(),
        client=client,
        settings=settings,
    )
    assert draft.generated_text == "Your premium went up by $340."
    assert draft.final_text is None
    assert client.calls[0]["model"] == "claude-sonnet-5"


def test_editing_a_draft_inserts_a_new_row(session, settings, comparison):
    original = generate_draft(
        session,
        comparison,
        _differences(),
        _breakdown(),
        client=FakeClient(),
        settings=settings,
    )
    edited = save_edit(session, original, "Here is what changed on your policy.")

    assert edited.id != original.id
    assert edited.generated_text == original.generated_text
    assert edited.final_text == "Here is what changed on your policy."
    assert edited.edited_at is not None
    session.refresh(original)
    assert original.final_text is None
    assert latest_draft(session, comparison.id).id == edited.id
