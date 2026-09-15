"""Who did it, end to end.

A signed-in request writes a row that points at that account. The gate is the
only place that knows who is asking, so every test here goes through it rather
than calling a service function with a user_id it made up.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import User
from renewal.web import create_app
from tests.authhelp import EMAIL, sign_in


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
        session_cookie_secure=False,
    )


@pytest.fixture
def app(engine, clean_db, settings):
    from renewal.background import InlineRunner

    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
        runner=InlineRunner(),
    )


@pytest.fixture
def signed(app, engine):
    client = TestClient(app)
    sign_in(client, engine)
    return client


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _me(db) -> User:
    return db.query(User).filter_by(email=EMAIL).one()


def test_the_gate_puts_the_user_id_on_the_request(app, engine, db):
    """Every later task reads it from here. Asserted through a route rather
    than by calling the middleware, because the state is what routes see."""
    from fastapi import Request

    seen = {}

    @app.get("/_whoami_test")
    def whoami(request: Request):
        from renewal.web.deps import acting_user_id

        seen["user_id"] = acting_user_id(request)
        return {"ok": True}

    client = TestClient(app)
    sign_in(client, engine)
    client.get("/_whoami_test")

    assert seen["user_id"] == _me(db).id


def test_an_unauthenticated_path_has_no_user(app):
    """acting_user_id reads through getattr with a default: the .ics feed and
    the mail webhook never pass the gate and have no user on their state."""
    from renewal.web.deps import acting_user_id

    class _Bare:
        state = type("S", (), {})()

    assert acting_user_id(_Bare()) is None
