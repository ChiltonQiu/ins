"""Production wiring for uvicorn.

The clock is started here rather than inside create_app, because create_app is
called a hundred times by the test suite and none of those applications should
raise a thread. DIGEST_TICK_SECONDS=0 turns it off, for an installation driving
`python -m scripts.digest` from cron instead.
"""

from sqlalchemy.orm import sessionmaker

from renewal.blobstore import store_from_settings
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.digest.clock import DigestClock
from renewal.providers import build_client
from renewal.web import create_app

_settings = load_settings()
_session_factory = sessionmaker(bind=get_engine())

app = create_app(
    settings=_settings,
    store=store_from_settings(_settings),
    model_client=build_client(_settings),
    session_factory=_session_factory,
)

if _settings.digest_tick_seconds > 0:
    DigestClock(
        _session_factory, _settings,
        tick_seconds=_settings.digest_tick_seconds,
    ).start()
