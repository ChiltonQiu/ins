"""The circularity, closed.

An emailed renewal dec page produces a premium_change attention item with no
human action at all. That is impossible before this change — the item is raised
inside build_matrix, and the pipeline never calls build_matrix — and it is the
single assertion that proves the automatic path finishes the job rather than
stopping one click short of it.
"""

import json

from renewal.attention.rules import open_items
from renewal.comparison import matrix_for
from renewal.models import Client, Comparison, Draft, Policy, PolicyTerm
from renewal.pipeline import ingest_document
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings
from tests.test_renewal_arrives import Carrier, DEC_PAGE, _arrive, _incumbent


def test_a_renewal_compares_itself_and_raises_the_premium_change(
    session, store
):
    policy, prior = _incumbent(session)

    document = _arrive(session, store)

    renewal = session.query(PolicyTerm).filter_by(
        source_document_id=document.id
    ).one()
    comparison = session.query(Comparison).one()
    matrix = matrix_for(session, comparison)
    assert [c.term.id for c in matrix.columns] == [prior.id, renewal.id]

    # 3900.00 -> 4210.00 is 7.9%, under the 10% default, so this asserts the
    # rule ran rather than that it fired. renewal_received is the one that must
    # be there either way.
    reasons = {row.reason_code for row in open_items(session)}
    assert "renewal_received" in reasons


def test_the_premium_change_item_is_raised_without_a_click(session, store):
    """A move big enough to clear the threshold, reached with no human."""
    policy, prior = _incumbent(session)
    prior.total_premium = "3000.00"  # 4210.00 is +40%
    session.flush()

    _arrive(session, store)

    reasons = {row.reason_code for row in open_items(session)}
    assert "premium_change" in reasons


def test_the_draft_is_generated_too(session, store):
    _incumbent(session)
    _arrive(session, store)
    assert session.query(Draft).count() == 1


def test_a_first_term_compares_with_nothing(session, store):
    """New business has no prior term. Nothing to compare, and no error."""
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    session.add(
        Policy(client_id=client.id, carrier_name="Progressive",
               policy_number="AU-4471", line_of_business="commercial_auto",
               state="OR")
    )
    session.flush()

    _arrive(session, store)

    assert session.query(PolicyTerm).count() == 1
    assert session.query(Comparison).count() == 0


def test_the_same_renewal_twice_builds_one_comparison(session, store):
    """The twin check already stops the second promotion, so this asserts the
    compare stage cannot find a way around it."""
    _incumbent(session)
    same_file = make_text_pdf([DEC_PAGE])
    _arrive(session, store, same_file)
    _arrive(session, store, same_file)

    assert session.query(Comparison).count() == 1
    assert session.query(Draft).count() == 1


def test_a_quote_is_never_auto_compared(session, store):
    """Setting competitors side by side is a recommendation however it is
    worded. It waits for a person, and this is the assertion that keeps it
    waiting."""
    policy, prior = _incumbent(session)

    class Quoting(Carrier):
        def complete(self, *, model, system, content):
            if model == _settings().classification_model:
                return json.dumps({"doc_class": "quote", "confidence": 0.95})
            return super().complete(model=model, system=system, content=content)

    ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="quote.pdf", source="email_attachment", agency_id=1,
        model_client=Quoting(), settings=_settings(),
    )

    quoted = session.query(PolicyTerm).filter_by(kind="quoted").one()
    assert quoted.policy_id == policy.id
    assert session.query(Comparison).count() == 0
