"""The clock that asks whether a summary is due.

A daemon thread inside the application, which is the same size as the intake
runner beside it: no worker process, no queue, nothing new to deploy or
monitor.

It has the same one failure mode, stated the same way: work in this process
dies with this process. A day the application is down for its entirety is a day
with no summary. That is survivable only because the answer is computed from
state rather than from an event — the next day's summary names the same
deadline, because the deadline is still there.

sleep is injected so that a test can prove the loop ticks without sleeping. A
test that sleeps to win a race is a test that fails on a slow machine.
"""

from __future__ import annotations

import logging
import threading
import time

from renewal.config import Settings
from renewal.digest.send import send_due_digest
from renewal.settings_store import effective

logger = logging.getLogger(__name__)


class DigestClock:
    def __init__(
        self,
        session_factory,
        settings: Settings,
        *,
        tick_seconds: int = 300,
        sleep=time.sleep,
        send=None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._tick_seconds = tick_seconds
        self._sleep = sleep
        self._send = send

    def tick(self) -> bool:
        """One pass. Its own session, and it commits: a summary whose row is
        rolled back is a summary that goes out again five minutes later."""
        with self._session_factory() as session:
            # Her settings, not the environment's. The thread never sees a
            # request, so this is the only place they can reach it.
            settings = effective(session, self._settings)
            sent = send_due_digest(session, settings=settings, send=self._send)
            session.commit()
            return sent

    def run(self) -> None:
        """Ticks once before sleeping, so a process that comes up at two in the
        afternoon sends the morning's summary at two in the afternoon."""
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a clock must outlive one bad night
                logger.exception("digest tick failed")
            self._sleep(self._tick_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="digest", daemon=True)
        thread.start()
        return thread
