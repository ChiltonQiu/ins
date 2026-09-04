from sqlalchemy.orm import sessionmaker

from renewal.blobstore import store_from_settings
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.providers import build_client
from renewal.web import create_app

_settings = load_settings()
app = create_app(
    settings=_settings,
    store=store_from_settings(_settings),
    model_client=build_client(_settings),
    session_factory=sessionmaker(bind=get_engine()),
)
