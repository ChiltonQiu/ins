from renewal.models import Document, DocumentText
from scripts.bulk_import import ImportStats, import_tree, walk_pdfs
from tests.pdfmaker import make_text_pdf


def _tree(tmp_path):
    """The archive lives in its own subdirectory: the `store` fixture puts its
    blobs under tmp_path, and those are .pdf files too — walking tmp_path
    itself would find them on the second pass."""
    tmp_path = tmp_path / "archive"
    (tmp_path / "2024" / "acme").mkdir(parents=True)
    (tmp_path / "2024" / "acme" / "dec.pdf").write_bytes(
        make_text_pdf([["Named Insured: Acme Landscaping LLC"]]))
    (tmp_path / "2024" / "notes.txt").write_text("not a pdf")
    (tmp_path / "2025").mkdir()
    (tmp_path / "2025" / "renewal.PDF").write_bytes(
        make_text_pdf([["Named Insured: Acme Landscaping LLC"]]))
    return tmp_path


def test_walk_finds_pdfs_recursively_and_case_insensitively(tmp_path):
    root = _tree(tmp_path)
    names = sorted(p.name for p in walk_pdfs(root))
    assert names == ["dec.pdf", "renewal.PDF"]


def test_import_stores_every_pdf_with_its_text(session, store, tmp_path):
    stats = import_tree(session, store, _tree(tmp_path), agency_id=1)
    assert stats.imported == 2
    assert session.query(Document).count() == 2
    assert session.query(DocumentText).count() == 2


def test_re_running_is_free(session, store, tmp_path):
    """Content addressing makes a re-import free; the second pass imports
    nothing and re-extracts nothing."""
    root = _tree(tmp_path)
    import_tree(session, store, root, agency_id=1)
    second = import_tree(session, store, root, agency_id=1)
    assert second.imported == 0
    assert second.skipped == 2
    assert session.query(Document).count() == 2


def test_an_unreadable_pdf_is_counted_and_does_not_stop_the_walk(
    session, store, tmp_path
):
    root = _tree(tmp_path)
    (root / "broken.pdf").write_bytes(b"not really a pdf")
    stats = import_tree(session, store, root, agency_id=1)
    assert stats.failed == 1
    assert stats.imported == 2
