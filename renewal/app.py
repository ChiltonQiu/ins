"""Production wiring for uvicorn.

The clocks are started here rather than inside create_app, because create_app
is called a hundred times by the test suite and none of those applications
should raise a thread. Either tick interval set to 0 turns that clock off, for
an installation driving `python -m scripts.digest` or
`python -m scripts.poll_mail` from cron instead.
"""

from sqlalchemy.orm import sessionmaker

from renewal.background import ThreadRunner
from renewal.blobstore import store_from_settings
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.digest.clock import DigestClock
from renewal.mail.clock import MailClock
from renewal.providers import build_client
from renewal.web import create_app

_settings = load_settings()
_session_factory = sessionmaker(bind=get_engine())
# Named rather than built inline: the mail clock hands attachments to the same
# pool the web application uses, and two pools would mean twice the configured
# worker count with nothing saying so.
_store = store_from_settings(_settings)
_model_client = build_client(_settings)
_runner = ThreadRunner(_settings.intake_workers)

app = create_app(
    settings=_settings,
    store=_store,
    model_client=_model_client,
    session_factory=_session_factory,
    runner=_runner,
)

if _settings.digest_tick_seconds > 0:
    DigestClock(
        _session_factory, _settings,
        tick_seconds=_settings.digest_tick_seconds,
    ).start()

# An unset IMAP_HOST is the default and turns polling off entirely, the same
# way an unset SMTP_HOST turns notifications off.
if _settings.imap_host and _settings.imap_poll_seconds > 0:
    MailClock(
        _session_factory, _store, _settings,
        tick_seconds=_settings.imap_poll_seconds,
        client=_model_client, runner=_runner,
    ).start()
