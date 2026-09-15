"""A renewal arrives through the pipe, end to end.

The claim this holds: a renewal dec page dropped in becomes a term, raises a
renewal_received item, compares itself against the prior term, and that item's
Compare button lands on the picker with both terms ticked.

Every stage it crosses is tested on its own elsewhere. This is the one test
that puts them in a line, because the value of any of them is that the line
runs.
"""

import json
from datetime import date

from renewal.attention.rules import open_items
from renewal.models import (
    Client,
    Comparison,
    Document,
    DocumentLink,
    Draft,
    Policy,
    PolicyTerm,
)
from renewal.pipeline import ingest_document
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings

DEC_PAGE = [
    "PROGRESSIVE COMMERCIAL AUTO",
    "Policy Number: AU-4471",
    "Total Policy Premium $4,210.00",
    "Effective Date: 07/01/2026",
    "Expiration Date: 07/01/2027",
]


class Carrier:
    """One client for three stages, told apart by the model each one asks
    for."""

    def complete(self, *, model, system, content):
        settings = _settings()
        if model == settings.classification_model:
            return json.dumps({"doc_class": "declarations", "confidence": 0.95})
        if model == settings.date_model:
            return json.dumps({"dates": []})
        return json.dumps(
            {
                "fields": [
                    {
                        "field_path": path,
                        "value": value,
                        "confidence": 0.97,
                        "source_page": 1,
                        "source_text": source,
                    }
                    for path, value, source in (
                        ("policy.total_premium", "4210.00",
                         "Total Policy Premium $4,210.00"),
                        ("policy.policy_number", "AU-4471",
                         "Policy Number: AU-4471"),
                        ("policy.effective_date", "2026-07-01",
                         "Effective Date: 07/01/2026"),
                        ("policy.expiration_date", "2027-07-01",
                         "Expiration Date: 07/01/2027"),
                    )
                ]
            }
        )


def _incumbent(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="commercial_auto",
        state="OR",
    )
    session.add(policy)
    session.flush()
    prior = PolicyTerm(
        policy_id=policy.id,
        kind="bound",
        carrier_name="Progressive",
        policy_number="AU-4471",
        effective_date=date(2025, 7, 1),
        expiration_date=date(2026, 7, 1),
        total_premium="3900.00",
    )
    session.add(prior)
    session.flush()
    return policy, prior


def _arrive(session, store, data=None):
    return ingest_document(
        session, store, data=data if data is not None else make_text_pdf([DEC_PAGE]),
        original_filename="renewal.pdf", source="email_attachment", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )


def test_a_renewal_dec_page_becomes_a_term_and_raises_one_item(session, store):
    policy, prior = _incumbent(session)
    document = _arrive(session, store)

    # It filed itself: exact policy-number match, the only auto-link D8 allows.
    link = session.query(DocumentLink).filter_by(document_id=document.id).one()
    assert link.policy_id == policy.id

    # It promoted, because nothing needed a human.
    renewal = (
        session.query(PolicyTerm)
        .filter_by(source_document_id=document.id)
        .one()
    )
    assert renewal.kind == "bound"
    assert renewal.total_premium == "4210.00"

    rows = [
        row for row in open_items(session)
        if row.reason_code == "renewal_received"
    ]
    assert len(rows) == 1
    assert rows[0].compare_url == (
        f"/policies/{policy.id}/compare?baseline={prior.id}&comparand={renewal.id}"
    )


def test_a_renewal_compares_itself(session, store):
    """This assertion used to read the other way.

    "Nothing is compared until she clicks" was the design until the click was
    found to be sitting in front of the notification that existed to prompt it
    — premium_change is raised inside build_matrix, which nothing but that
    click ever called. A renewal is one policy\'s own history and the
    arithmetic is the same whoever asks for it, so it is built when the term
    lands. A quote still waits: see tests/test_auto_renewal.py.
    """
    _incumbent(session)
    _arrive(session, store)
    assert session.query(Comparison).count() == 1
    assert session.query(Draft).count() == 1


def test_the_same_file_delivered_twice_promotes_once(session, store):
    """A carrier sending the renewal by email and again through the portal is
    the ordinary case, not the strange one.

    Content addressing deduplicates the blob but deliberately not the
    document, so the second delivery is a second document with its own
    extraction. Promoting it too would put two identical terms on the chain
    and raise the renewal twice.
    """
    _incumbent(session)
    same_file = make_text_pdf([DEC_PAGE])
    _arrive(session, store, same_file)
    _arrive(session, store, same_file)

    assert session.query(Document).count() == 2  # two deliveries
    assert session.query(PolicyTerm).count() == 2  # prior plus one renewal
    assert len([
        row for row in open_items(session)
        if row.reason_code == "renewal_received"
    ]) == 1
