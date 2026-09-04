from datetime import date

from renewal.models import Client, Carrier, DocumentClassification, DocumentLink, Policy
from renewal.search.query import search


def _seed(session, store, text_lines, client_name="Acme Landscaping LLC"):
    from renewal.pipeline import ingest_document
    from tests.pdfmaker import make_text_pdf
    from tests.test_dates_llm import StubClient, _settings

    client = Client(display_name=client_name)
    session.add(client)
    session.flush()
    document = ingest_document(
        session, store, data=make_text_pdf([text_lines]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=StubClient('{"dates": []}'), settings=_settings(),
    )
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="manual", confidence=1.0, candidates=[]))
    session.flush()
    return client, document


def test_finds_a_document_by_a_word_in_its_text(session, store):
    _, document = _seed(session, store, ["NOTICE OF CANCELLATION", "effective soon"])
    results = search(session, "cancellation")
    assert [r.document_id for r in results] == [document.id]


def test_finds_a_document_by_its_clients_name(session, store):
    _, document = _seed(session, store, ["a page with no distinguishing words"])
    assert [r.document_id for r in search(session, "Acme Landscaping")] == [document.id]


def test_finds_a_document_by_policy_number(session, store):
    client, document = _seed(session, store, ["nothing useful here"])
    policy = Policy(client_id=client.id, carrier_name="Travelers",
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    # Append a corrected link rather than editing the first one. Tests model the
    # invariant too; a test that updates teaches the wrong pattern.
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             policy_id=policy.id, method="manual",
                             confidence=1.0, candidates=[]))
    session.flush()
    assert [r.document_id for r in search(session, "CAP-7781-22")] == [document.id]


def test_the_snippet_highlights_the_term(session, store):
    _seed(session, store, ["NOTICE OF CANCELLATION effective 07/01/2026"])
    snippet = search(session, "cancellation")[0].snippet
    assert "<b>" in snippet.lower() or "<mark>" in snippet.lower()


def test_results_are_newest_first(session, store):
    _, first = _seed(session, store, ["cancellation one"])
    _, second = _seed(session, store, ["cancellation two"], "Other Client LLC")
    assert [r.document_id for r in search(session, "cancellation")] == [
        second.id, first.id]


def test_filters_by_client(session, store):
    client, document = _seed(session, store, ["cancellation one"])
    _seed(session, store, ["cancellation two"], "Other Client LLC")
    assert [r.document_id for r in search(session, "cancellation",
                                          client_id=client.id)] == [document.id]


def test_filters_by_document_class(session, store):
    _, document = _seed(session, store, ["cancellation one"])
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="cancellation_notice",
                                       confidence=0.9,
                                       classifier_version="classify-v1",
                                       model_id="stub"))
    session.flush()
    assert len(search(session, "cancellation", doc_class="cancellation_notice")) == 1
    assert search(session, "cancellation", doc_class="invoice") == []


def test_an_unclassified_document_is_still_findable(session, store):
    """Search must never depend on classification having run."""
    _, document = _seed(session, store, ["cancellation one"])
    assert [r.document_id for r in search(session, "cancellation")] == [document.id]


def test_an_unmatched_document_is_still_findable(session, store):
    """Nor on client resolution having succeeded."""
    from renewal.pipeline import ingest_document
    from tests.pdfmaker import make_text_pdf
    from tests.test_dates_llm import StubClient, _settings
    document = ingest_document(
        session, store, data=make_text_pdf([["orphan cancellation notice"]]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=StubClient('{"dates": []}'), settings=_settings(),
    )
    result = search(session, "cancellation")[0]
    assert result.document_id == document.id
    assert result.client_name is None


def test_multi_word_queries_are_handled_as_a_phrase_search(session, store):
    _seed(session, store, ["NOTICE OF CANCELLATION"])
    assert search(session, '"notice of cancellation"')
    assert search(session, "notice cancellation")


def test_a_query_with_no_hits_returns_nothing_rather_than_everything(session, store):
    _seed(session, store, ["cancellation one"])
    assert search(session, "zzzznotaword") == []


def test_punctuation_in_a_query_does_not_error(session, store):
    """websearch_to_tsquery tolerates what plainto_tsquery does not."""
    _seed(session, store, ["cancellation one"])
    assert search(session, "cancellation & | ! ()") is not None


def test_one_document_appears_once_even_when_it_matches_two_ways(session, store):
    _seed(session, store, ["Acme Landscaping cancellation"])
    assert len(search(session, "Acme")) == 1


def test_a_deduplicated_document_matching_on_text_records_why(session, store):
    _seed(session, store, ["NOTICE OF CANCELLATION"])
    assert "text" in search(session, "cancellation")[0].matched_on


def test_an_empty_query_returns_nothing(session, store):
    _seed(session, store, ["cancellation one"])
    assert search(session, "   ") == []
