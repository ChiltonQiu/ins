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
    if trans.is_active:
        trans.rollback()
    conn.close()


@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs")


TABLES = (
    "client, policy, policy_term, coverage, insured_item, document, extraction,"
    " extracted_field, correction, renewal_run, comparison, difference,"
    " reclassification, draft, carrier, carrier_alias, carrier_admitted_status,"
    " policy_billing_type, document_text, document_classification,"
    " document_link, document_date, date_event, manual_date, manual_date_event,"
    " inbound_message, attention_item, attention_event, app_user, user_session"
)


@pytest.fixture
def clean_db(engine):
    """For tests that commit (the web tests). The `session` fixture rolls back,
    but a committing test would otherwise leak rows into the next one."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        # agency is reference data seeded by a migration, not test data. If it
        # ever lands in TABLES the truncate would delete the row and reset the
        # sequence, so every later test using agency_id=1 would fail somewhere
        # far from the cause. Fail here instead.
        assert conn.execute(text("SELECT count(*) FROM agency")).scalar() == 1
    yield
