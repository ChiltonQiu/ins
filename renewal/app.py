"""Production wiring for uvicorn.

The clocks are started here rather than inside create_app, because create_app
is called a hundred times by the test suite and none of those applications
should raise a thread. Either tick interval set to 0 turns that clock off, for
an installation driving `python -m scripts.digest` or
`python -m scripts.poll_mail` from cron instead.

The mail clock always starts otherwise: /setup can turn mail on after boot,
and a tick with no mailbox configured returns at once.
"""

from sqlalchemy.orm import sessionmaker

from renewal.background import ThreadRunner
from renewal.blobstore import store_from_settings
from renewal.db import get_engine
from renewal.digest.clock import DigestClock
from renewal.live import Live
from renewal.mail.clock import MailClock
from renewal.updates import UpdateClock
from renewal.web import create_app

# First, before anything reads .env: which keys the real environment set is
# decided from what is in os.environ now. With no key, this boots on an
# UnconfiguredClient and /setup is where the key comes from.
_live = Live.from_process()
_settings = _live.settings
_session_factory = sessionmaker(bind=get_engine())
# Named rather than built inline: the mail clock hands attachments to the same
# pool the web application uses, and two pools would mean twice the configured
# worker count with nothing saying so.
_store = store_from_settings(_settings)
_runner = ThreadRunner(_settings.intake_workers)

app = create_app(
    store=_store,
    session_factory=_session_factory,
    runner=_runner,
    live=_live,
)

if _settings.digest_tick_seconds > 0:
    DigestClock(
        _session_factory, _settings,
        tick_seconds=_settings.digest_tick_seconds, live=_live,
    ).start()

if _settings.imap_poll_seconds > 0:
    MailClock(
        _session_factory, _store, _settings,
        tick_seconds=_settings.imap_poll_seconds,
        runner=_runner, live=_live,
    ).start()

# Once at start, then every twelve hours; UPDATE_CHECK=false makes each tick
# a no-op.
UpdateClock(_settings).start()
