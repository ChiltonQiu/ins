from renewal.models import Client, DocumentLink, Policy
from renewal.pipeline import ingest_document
from renewal.resolve.service import (
    assign, latest_link, resolve_document, unmatched,
)
from tests.pdfmaker import make_text_pdf

DEC_LINES = [
    "DECLARATIONS PAGE",
    "Named Insured: Acme Landscaping LLC",
    "Policy Number: CAP-7781-22",
    "Effective Date: 07/01/2025",
]


def _document(session, store, lines=DEC_LINES):
    return ingest_document(session, store, data=make_text_pdf([lines]),
                           original_filename="dec.pdf", source="bulk_import",
                           agency_id=1)


def _client_with_policy(session):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name="Travelers",
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    return client, policy


def test_an_exact_policy_number_auto_links(session, store):
    client, policy = _client_with_policy(session)
    document = _document(session, store)
    link = resolve_document(session, document)
    assert link is not None
    assert (link.client_id, link.policy_id, link.method) == (
        client.id, policy.id, "auto")


def test_a_name_only_match_does_not_auto_link(session, store):
    """It queues instead, with the candidates recorded for her to choose from."""
    _client_with_policy(session)
    document = _document(session, store, [
        "Named Insured: Acme Landscaping LLC", "Renewal offer enclosed."])
    assert resolve_document(session, document) is None
    assert document.id in {d.id for d in unmatched(session)}


def test_two_exact_matches_do_not_auto_link(session, store):
    """Ambiguity is queued, never resolved silently."""
    client_a, _ = _client_with_policy(session)
    client_b = Client(display_name="Acme Landscaping Inc")
    session.add(client_b)
    session.flush()
    session.add(Policy(client_id=client_b.id, carrier_name="Travelers",
                       policy_number="CAP-7781-22",
                       line_of_business="commercial_auto"))
    session.flush()
    document = _document(session, store)
    assert resolve_document(session, document) is None


def test_manual_assignment_supersedes_and_records_what_was_offered(session, store):
    client, policy = _client_with_policy(session)
    wrong = Client(display_name="Wrong Client LLC")
    session.add(wrong)
    session.flush()
    document = _document(session, store)
    resolve_document(session, document)

    assign(session, document.id, client_id=wrong.id, policy_id=None,
           candidates=[{"client_id": client.id, "policy_id": policy.id,
                        "score": 1.0, "reason": "exact policy number"}])

    rows = session.query(DocumentLink).filter_by(
        document_id=document.id).order_by(DocumentLink.id).all()
    assert [r.method for r in rows] == ["auto", "manual"]
    assert latest_link(session, document.id).client_id == wrong.id
    assert rows[-1].candidates[0]["reason"] == "exact policy number"


def test_a_linked_document_is_not_in_the_queue(session, store):
    _client_with_policy(session)
    document = _document(session, store)
    resolve_document(session, document)
    assert document.id not in {d.id for d in unmatched(session)}


def test_resolution_is_not_repeated_for_an_already_linked_document(session, store):
    _client_with_policy(session)
    document = _document(session, store)
    resolve_document(session, document)
    resolve_document(session, document)
    assert session.query(DocumentLink).filter_by(document_id=document.id).count() == 1
