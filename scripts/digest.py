"""One tick of the daily summary, then exit.

For an installation that would rather drive this from cron or a systemd timer
than from the application's own thread. Same code and the same once-a-day
guarantee: the notification_send row is what remembers, so a cron entry and a
running application cannot between them send two.

    */5 * * * * cd /srv/renewal && .venv/bin/python -m scripts.digest

Set DIGEST_TICK_SECONDS=0 if you do this, or the application's own clock is
doing the same work beside it.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from renewal.config import load_settings
from renewal.db import get_engine
from renewal.digest.clock import DigestClock


def main() -> int:
    settings = load_settings()
    clock = DigestClock(sessionmaker(bind=get_engine()), settings)
    print("sent" if clock.tick() else "nothing due")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
