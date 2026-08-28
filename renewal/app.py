from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.extract.runner import AnthropicClient
from renewal.web import create_app
from sqlalchemy.orm import sessionmaker

_settings = load_settings()
app = create_app(
    settings=_settings,
    store=BlobStore(_settings.blob_root),
    model_client=AnthropicClient(_settings.anthropic_api_key),
    session_factory=sessionmaker(bind=get_engine()),
)
