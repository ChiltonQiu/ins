import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg:///renewal_test"
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(Path(__file__).parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """Each test runs in a transaction that is rolled back afterwards."""
    conn = engine.connect()
    trans = conn.begin()
    sess = sessionmaker(bind=conn)()
    yield sess
    sess.close()
    trans.rollback()
    conn.close()


@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs")


TABLES = (
    "client, policy, policy_term, coverage, insured_item, document, extraction,"
    " extracted_field, correction, renewal_run, comparison, difference,"
    " reclassification, draft"
)


@pytest.fixture
def clean_db(engine):
    """For tests that commit (the web tests). The `session` fixture rolls back,
    but a committing test would otherwise leak rows into the next one."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield
