import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Client, DocumentLink
from renewal.pipeline import ingest_document
from renewal.web import create_app
from tests.authhelp import sign_in
from tests.pdfmaker import make_text_pdf


@pytest.fixture
def client_app(engine, clean_db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
        session_cookie_secure=False,
    )
    app = create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
    )
    with TestClient(app) as test_client:
        sign_in(test_client, engine)
        yield test_client


def _seed(engine, tmp_path, lines, client_name="Acme Landscaping LLC"):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name=client_name)
    sess.add(client)
    sess.flush()
    document = ingest_document(
        sess, BlobStore(tmp_path / "blobs"), data=make_text_pdf([lines]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
    )
    sess.add(DocumentLink(document_id=document.id, client_id=client.id,
                          method="manual", confidence=1.0, candidates=[]))
    sess.commit()
    got = document.id
    sess.close()
    return got


@pytest.fixture
def seeded_doc(engine, tmp_path):
    yield _seed(engine, tmp_path, ["NOTICE OF CANCELLATION effective 07/01/2026"])


@pytest.fixture
def seeded_script(engine, tmp_path):
    """A document whose own text is markup. ts_headline will hand it back
    wrapped in <mark> tags, and the rest of it is not ours to trust."""
    yield _seed(engine, tmp_path,
                ["<script>alert(1)</script> payload here"])


def test_the_box_renders_empty(client_app):
    response = client_app.get("/search")
    assert response.status_code == 200
    assert "<form" in response.text


def test_a_query_returns_results_with_a_highlighted_snippet(client_app, seeded_doc):
    body = client_app.get("/search?q=cancellation").text
    assert "<mark>" in body
    assert "Acme Landscaping LLC" in body


def test_the_snippet_is_escaped_apart_from_the_highlight(client_app, seeded_script):
    """ts_headline returns markup, so the rest must not be trusted as HTML.

    base.html carries a legitimate <script> for the theme, so the assertion is
    on the snippet itself: the document's own tags must arrive escaped, and the
    highlight must still be a real tag."""
    body = client_app.get("/search?q=payload").text
    snippet = body.split('<p class="snippet">', 1)[1].split("</p>", 1)[0]
    assert "<script>" not in snippet
    assert "<mark>" in snippet


def test_the_headline_filter_escapes_everything_but_the_highlight():
    """ts_headline picks its own fragment, so whether a given tag reaches the
    snippet is up to Postgres. The escaping rule is pinned on the filter."""
    from renewal.web.templating import _headline

    got = str(_headline("<script>alert(1)</script> <mark>payload</mark>"))
    assert got == "&lt;script&gt;alert(1)&lt;/script&gt; <mark>payload</mark>"


def test_the_headline_filter_does_not_let_a_forged_mark_through():
    """A document containing the literal text &lt;mark&gt; must not be able to
    close the real highlight early."""
    from renewal.web.templating import _headline

    got = str(_headline("a <b>bold</b> claim"))
    assert "<b>" not in got


def test_no_results_says_so(client_app, seeded_doc):
    assert "no matches" in client_app.get("/search?q=zzzznotaword").text.lower()


def test_filters_are_reflected_back_into_the_form(client_app, seeded_doc):
    body = client_app.get("/search?q=cancellation&doc_class=invoice").text
    assert 'value="cancellation"' in body


def test_an_empty_query_does_not_list_every_document(client_app, seeded_doc):
    """The client name still appears in the filter dropdown, so the assertion
    is on the results list, which must not be rendered at all."""
    body = client_app.get("/search").text
    assert '<ul class="results">' not in body
    assert "no matches" not in body.lower()
