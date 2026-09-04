"""The 300ms budget as a test rather than an aspiration.

Marked `perf` and excluded from the default run: it seeds ten thousand
documents, which takes far longer than the assertion it makes.
Run with: pytest -m perf tests/test_search_perf.py -s
"""

import time

import pytest
from sqlalchemy import select, text

from renewal.models import Client, Document, DocumentText
from renewal.search.query import search

BUDGET_SECONDS = 0.3
DOCUMENTS = 10_000


@pytest.mark.perf
def test_search_stays_under_the_budget_at_ten_thousand_documents(session, capsys):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    session.bulk_save_objects([
        Document(blob_sha256=f"{i:064x}", original_filename=f"{i}.pdf",
                 page_count=1, has_text_layer=True, doc_type="dec_page",
                 source="bulk_import", agency_id=1)
        for i in range(DOCUMENTS)
    ])
    session.flush()
    ids = [row[0] for row in session.execute(select(Document.id))]
    session.bulk_save_objects([
        DocumentText(document_id=document_id, page_number=1,
                     text=f"policy declarations page number {document_id} "
                          f"cancellation notice effective 07/01/2026",
                     extraction_method="pymupdf", extractor_version="text-v1")
        for document_id in ids
    ])
    session.flush()
    session.execute(text("ANALYZE document_text"))

    started = time.perf_counter()
    results = search(session, "cancellation", limit=50)
    elapsed = time.perf_counter() - started

    with capsys.disabled():
        print(f"\nsearch over {DOCUMENTS} documents: {1000 * elapsed:.0f}ms")
    assert results
    assert elapsed < BUDGET_SECONDS
