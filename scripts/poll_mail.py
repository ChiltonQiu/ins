"""One poll of the mailbox, then exit.

For an installation that would rather drive this from cron or a systemd timer
than from the application's own thread. Same code and the same guarantees: the
folder is opened read-only and dedupe is by Message-ID, so a cron entry and a
running application cannot between them ingest the same message twice.

    */5 * * * * cd /srv/renewal && .venv/bin/python -m scripts.poll_mail

Set IMAP_POLL_SECONDS=0 if you do this, or the application's own clock is doing
the same work beside it.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from renewal.background import ThreadRunner
from renewal.blobstore import store_from_settings
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.mail.clock import MailClock
from renewal.providers import build_client


def main() -> int:
    settings = load_settings()
    if not settings.imap_host:
        print("IMAP_HOST is not set: polling is off")
        return 0

    runner = ThreadRunner(settings.intake_workers)
    clock = MailClock(
        sessionmaker(bind=get_engine()), store_from_settings(settings), settings,
        client=build_client(settings), runner=runner,
    )
    result = clock.tick()
    print(
        f"seen {result.seen}, ingested {result.ingested}, "
        f"already had {result.duplicate}, failed {result.failed}"
    )
    # Attachments were handed to the pool; without this the process exits while
    # they are still being read.
    runner.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
