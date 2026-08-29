from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.providers import build_client
from renewal.web import create_app
from sqlalchemy.orm import sessionmaker

_settings = load_settings()
app = create_app(
    settings=_settings,
    store=BlobStore(_settings.blob_root),
    model_client=build_client(_settings),
    session_factory=sessionmaker(bind=get_engine()),
)
