"""Watching, and the limits on what watching may record.

The load-bearing test in this file is the one that asserts a client's name
cannot reach the export. Everything else here is mechanics; that one is the
reason the feature is allowed to exist, because the file it produces is meant
to leave the agency.
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.ingest import ingest_pdf
from renewal.models import Client, Policy, PolicyTerm, UsageEvent
from renewal.usagestats import report
from tests.pdfmaker import make_text_pdf
from tests.test_web_inbox import app, settings, signed  # noqa: F401


def _events(engine):
    session = sessionmaker(bind=engine)()
    try:
        return list(session.scalars(select(UsageEvent).order_by(UsageEvent.id)))
    finally:
        session.close()


def test_a_page_view_is_recorded_as_its_template_not_its_path(
    signed, engine, settings
):
    """The difference between one row that aggregates and forty rows, one of
    which names a document."""
    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["anything"]]), original_filename="a.pdf",
            source="manual_upload", agency_id=1,
        )
        session.commit()
        document_id = document.id
    finally:
        session.close()

    with signed() as client:
        client.get(f"/documents/{document_id}/review")

    routes = [e.route for e in _events(engine)]
    assert "/documents/{document_id}/review" in routes
    assert f"/documents/{document_id}/review" not in routes


def test_the_referer_becomes_a_template_or_nothing(signed, engine):
    """A foreign referer, or a crafted one, must never become a row: what is
    stored is one of this application's own templates, or nothing."""
    with signed() as client:
        client.get("/inbox", headers={"referer": "http://testserver/"})
        client.get("/search", headers={"referer": "https://evil.example/x?y=1"})
        client.get("/clients", headers={"referer": "http://testserver/nope"})

    came_from = {e.route: e.from_route for e in _events(engine)}
    assert came_from["/inbox"] == "/"
    # Another origin's page is not one of ours and cannot name one.
    assert came_from["/search"] is None
    # Ours, but matching no route: there is no template to record.
    assert came_from["/clients"] is None


def test_a_download_and_a_static_file_are_traffic_not_use(
    signed, engine, settings
):
    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["anything"]]), original_filename="a.pdf",
            source="manual_upload", agency_id=1,
        )
        session.commit()
        document_id = document.id
    finally:
        session.close()

    with signed() as client:
        client.get(f"/documents/{document_id}")      # the PDF itself
        client.get("/static/app.css")

    routes = [e.route for e in _events(engine)]
    assert not any(r.startswith("/static") for r in routes)
    assert "/documents/{document_id}" not in routes


def test_tracking_can_be_turned_off(engine, settings, tmp_path):
    from fastapi.testclient import TestClient

    from renewal.web import create_app

    quiet = settings.__class__(**{**settings.__dict__, "usage_tracking": False})
    application = create_app(
        settings=quiet, store=BlobStore(quiet.blob_root), model_client=None,
        session_factory=sessionmaker(bind=engine),
    )
    with TestClient(application) as client:
        client.get("/login")

    assert _events(engine) == []


def test_the_export_cannot_carry_a_client_name_or_a_filename(
    signed, engine, settings
):
    """The whole reason this feature is allowed to exist.

    The file is meant to be mailed, pasted into a chat, handed to a model. If
    a client's name can ride along, none of that is safe and the feature is a
    liability rather than a diagnosis.
    """
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Ramirez Landscaping Incorporated")
        session.add(client_row)
        session.flush()
        policy = Policy(client_id=client_row.id, carrier_name="Travelers",
                        policy_number="SECRET-POLICY-9001",
                        line_of_business="property")
        session.add(policy)
        session.flush()
        session.add(PolicyTerm(policy_id=policy.id, total_premium="98765"))
        ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["Named Insured: Ramirez Landscaping"]]),
            original_filename="ramirez-confidential-dec.pdf",
            source="manual_upload", agency_id=1,
        )
        session.commit()
    finally:
        session.close()

    with signed() as client:
        client.get("/")
        client.get("/inbox")

    session = sessionmaker(bind=engine)()
    try:
        text = json.dumps(report(session, days=30))
    finally:
        session.close()

    assert "Ramirez" not in text
    assert "SECRET-POLICY-9001" not in text
    assert "ramirez-confidential-dec.pdf" not in text
    assert "98765" not in text
    assert "Travelers" not in text


def test_the_report_runs_on_an_empty_database(engine):
    session = sessionmaker(bind=engine)()
    try:
        data = report(session, days=7)
    finally:
        session.close()

    assert data["schema_version"] == 1
    assert data["intake"]["total"] == 0
    assert json.dumps(data)  # it has to serialise, which is the point of it


def test_the_report_counts_what_the_pipeline_did(signed, engine, settings):
    session = sessionmaker(bind=engine)()
    try:
        ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["Expiration Date: 07/01/2027"]]),
            original_filename="dec.pdf", source="email_attachment", agency_id=1,
        )
        session.commit()
    finally:
        session.close()

    with signed() as client:
        client.get("/")

    session = sessionmaker(bind=engine)()
    try:
        data = report(session, days=30)
    finally:
        session.close()

    assert data["intake"]["documents_by_source"]["email_attachment"] == 1
    assert data["usage"]["page_views"] >= 1
    assert "/" in data["usage"]["by_route"]
