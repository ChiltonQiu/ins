"""The background path, and the one failure mode it is allowed to have.

In-process background work dies with the process. That is a real failure mode
and the design makes it visible rather than engineering it away — but it must
be the ONLY way a document is left in 'processing'. An ordinary exception has
to land on 'failed', which is a state the inbox explains, not a state that
looks like work still happening.
"""

from sqlalchemy.orm import sessionmaker

from renewal.background import InlineRunner, process_document
from renewal.ingest import ingest_pdf
from renewal.models import Document, DocumentText
from tests.pdfmaker import make_text_pdf


def _pending(engine, store) -> int:
    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, store, data=make_text_pdf([["Expiration Date: 07/01/2026"]]),
            original_filename="dec.pdf", source="manual_upload", agency_id=1,
        )
        document.status = "processing"
        session.commit()
        return document.id
    finally:
        session.close()


def test_processing_becomes_processed_and_the_stages_ran(engine, clean_db, store):
    document_id = _pending(engine, store)
    factory = sessionmaker(bind=engine)

    process_document(factory, store, document_id)

    session = factory()
    try:
        assert session.get(Document, document_id).status == "processed"
        assert session.query(DocumentText).filter_by(
            document_id=document_id
        ).count() == 1
    finally:
        session.close()


def test_a_raising_run_lands_on_failed_not_processing(
    engine, clean_db, store, monkeypatch
):
    """'processing' means a restart ate it. An exception is a different thing
    and has to say so."""
    import renewal.background as background

    def boom(*args, **kwargs):
        raise RuntimeError("the whole stage list exploded")

    monkeypatch.setattr(background, "run_stages", boom)
    document_id = _pending(engine, store)
    factory = sessionmaker(bind=engine)

    process_document(factory, store, document_id)

    session = factory()
    try:
        assert session.get(Document, document_id).status == "failed"
    finally:
        session.close()


def test_a_missing_document_is_not_an_error(engine, clean_db, store):
    """The row can be gone by the time the task runs. Nothing to do."""
    process_document(sessionmaker(bind=engine), store, 999_999)


def test_the_inline_runner_runs_it_now(engine, clean_db, store):
    """What the tests use, so a web test's assertions can run straight after
    the request instead of racing a thread."""
    seen = []
    InlineRunner().submit(seen.append, "ran")
    assert seen == ["ran"]


def test_status_changed_at_moves_with_the_status(engine, clean_db, store):
    """Stalled is measured from this, so a retry must reset the clock."""
    document_id = _pending(engine, store)
    factory = sessionmaker(bind=engine)

    session = factory()
    try:
        before = session.get(Document, document_id).status_changed_at
    finally:
        session.close()

    process_document(factory, store, document_id)

    session = factory()
    try:
        assert session.get(Document, document_id).status_changed_at > before
    finally:
        session.close()
