"""The clock that asks the mailbox whether anything arrived.

The same shape as the digest clock beside it, and the same one failure mode
stated the same way: work in this process dies with this process. That costs
less here than anywhere else in the application — a poll that never happened
leaves the mail exactly where it was, and the next poll finds it.

Attachments go to the intake runner rather than being read inside the poll.
Reading a scanned PDF is OCR plus three model calls, and doing that inline
would hold the mailbox connection open for minutes while it ran.
"""

from __future__ import annotations

import logging
import threading
import time

from renewal.background import process_document
from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.mail.poll import poll_once, open_mailbox

logger = logging.getLogger(__name__)


class MailClock:
    def __init__(
        self,
        session_factory,
        store: BlobStore,
        settings: Settings,
        *,
        tick_seconds: int = 300,
        sleep=time.sleep,
        client=None,
        runner=None,
        opener=open_mailbox,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._settings = settings
        self._tick_seconds = tick_seconds
        self._sleep = sleep
        self._client = client
        self._runner = runner
        self._opener = opener

    def _hand_off(self, document_id: int) -> None:
        """Where a stored attachment goes to be read.

        With no runner the document is left at 'processing' and the inbox shows
        it as stalled with a Retry button, which is the existing contract for
        work that was never picked up rather than a new failure mode.
        """
        if self._runner is None:
            return
        self._runner.submit(
            process_document, self._session_factory, self._store, document_id,
            model_client=self._client, settings=self._settings,
        )

    def tick(self):
        """One poll. Its own session, and it commits: a message whose rows are
        rolled back is a message the next poll ingests again."""
        with self._session_factory() as session:
            result = poll_once(
                session, self._store, settings=self._settings,
                client=self._client, on_document=self._hand_off,
                opener=self._opener,
            )
            session.commit()
            return result

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a clock outlives one bad night
                logger.exception("mail poll tick failed")
            self._sleep(self._tick_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="mailpoll", daemon=True)
        thread.start()
        return thread
