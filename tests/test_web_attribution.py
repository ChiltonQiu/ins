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


def _extracted_field(db):
    from renewal.models import Document, ExtractedField, Extraction

    document = Document(blob_sha256="a" * 64, original_filename="a.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        agency_id=1)
    db.add(document)
    db.flush()
    extraction = Extraction(document_id=document.id, extractor_version="v1",
                            model_id="m", status="ok")
    db.add(extraction)
    db.flush()
    field = ExtractedField(extraction_id=extraction.id,
                           field_path="policy.number", value="P-1",
                           confidence=0.9)
    db.add(field)
    db.commit()
    return field


def test_a_correction_records_who_made_it(signed, db):
    """The training-data one: a corrections table that cannot tell a senior
    producer's correction from a temp's typo is not training data."""
    from renewal.models import Correction

    field = _extracted_field(db)
    response = signed.post(f"/fields/{field.id}/correct",
                           data={"corrected_value": "P-2"})
    assert response.status_code == 204

    correction = db.query(Correction).one()
    assert correction.user_id == _me(db).id


def _document_date(db):
    from datetime import date

    from renewal.models import Document, DocumentDate

    document = Document(blob_sha256="b" * 64, original_filename="b.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        agency_id=1)
    db.add(document)
    db.flush()
    row = DocumentDate(
        document_id=document.id, date_value=date(2026, 12, 1),
        date_type="policy_expiration", source_page=1, source_text="Expires",
        confidence=0.9, extractor_version="dates-regex-v1", pass_name="regex",
    )
    db.add(row)
    db.commit()
    return row


def test_confirming_a_date_records_who_confirmed_it(signed, db):
    from renewal.models import DateEvent

    row = _document_date(db)
    assert signed.post(f"/dates/{row.id}/confirm").status_code in (200, 303)

    event = db.query(DateEvent).one()
    assert event.action == "confirmed"
    assert event.user_id == _me(db).id


def test_adding_a_date_by_hand_records_who_added_it(signed, db):
    from renewal.models import ManualDate

    response = signed.post("/manual-dates", data={
        "title": "Call the carrier", "date_value": "2026-12-01",
        "date_type": "other",
    })
    assert response.status_code in (200, 303)

    added = db.query(ManualDate).one()
    assert added.user_id == _me(db).id
