"""Intake off the request thread.

No worker process, no queue, no scheduler. A thread pool inside the application
is the whole mechanism, which is the right size for an agency of a few people
and adds nothing to deploy.

It has one real failure mode: work in this process dies with this process, so a
restart can leave a document in 'processing' forever. The design makes that
visible — the inbox shows it as stalled with a Retry button — rather than
pretending it cannot happen. Every stage is idempotent, so retrying is safe by
construction.

An ordinary exception is NOT that failure mode and must not look like it. It
lands on 'failed', which the inbox explains and offers a retry for, because a
document that says 'working on it' forever while nothing is working is the one
outcome worse than a visible error.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Document
from renewal.pipeline import run_stages
from renewal.providers import ModelClient

logger = logging.getLogger(__name__)


class InlineRunner:
    """Runs the work immediately, on the calling thread.

    What the tests use. A web test asserting on what intake produced would
    otherwise be racing a thread, and a test that sleeps to win that race is a
    test that fails on a slow machine.
    """

    def submit(self, fn, /, *args, **kwargs) -> None:
        fn(*args, **kwargs)


class ThreadRunner:
    """What the application uses.

    Two workers: intake is mostly waiting on a model provider, and a third
    concurrent extraction buys nothing for one person dropping in a file.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="intake"
        )

    def submit(self, fn, /, *args, **kwargs) -> None:
        self._pool.submit(fn, *args, **kwargs)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _set_status(session, document: Document, status: str) -> None:
    document.status = status
    document.status_changed_at = datetime.now(timezone.utc)
    session.commit()


def process_document(
    session_factory,
    store: BlobStore,
    document_id: int,
    *,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
    acknowledged: frozenset[str] = frozenset(),
) -> None:
    """Run the stages for one document and record where it ended up.

    Its own session: the request's session is closed by the time this runs.
    """
    session = session_factory()
    try:
        document = session.get(Document, document_id)
        if document is None:
            # The row can be gone by the time the task runs. Nothing to do,
            # and nothing wrong.
            logger.info("background skip document_id=%s reason=missing", document_id)
            return
        try:
            run_stages(
                session,
                store,
                document,
                model_client=model_client,
                settings=settings,
                acknowledged=acknowledged,
            )
            _set_status(session, document, "processed")
            logger.info("background done document_id=%s", document_id)
        except Exception:  # noqa: BLE001 - the status must never be left behind
            logger.exception("background failed document_id=%s", document_id)
            session.rollback()
            document = session.get(Document, document_id)
            if document is not None:
                _set_status(session, document, "failed")
    finally:
        session.close()
