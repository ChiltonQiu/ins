import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.carriers import admitted_status, resolve_carrier
from renewal.models import (
    Agency, Carrier, Client, Policy, PolicyBillingType,
)
from renewal.web import create_app


@pytest.fixture
def client_app(engine, clean_db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )
    app = create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(engine):
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture
def agency_token(engine):
    sess = sessionmaker(bind=engine)()
    token = sess.query(Agency).one().ics_token
    sess.close()
    yield token


def test_the_ics_feed_needs_the_token(client_app):
    assert client_app.get("/calendar/not-the-token.ics").status_code == 404


def test_the_ics_feed_serves_with_the_token(client_app, agency_token):
    response = client_app.get(f"/calendar/{agency_token}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")


def test_regenerating_the_token_kills_the_old_link(client_app, agency_token):
    client_app.post("/settings/regenerate-ics-token", follow_redirects=False)
    assert client_app.get(f"/calendar/{agency_token}.ics").status_code == 404


def test_the_settings_page_warns_that_the_link_is_a_credential(client_app):
    body = client_app.get("/settings").text
    assert "anyone with this link" in body.lower()



@pytest.fixture
def carrier_id(engine):
    sess = sessionmaker(bind=engine)()
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    sess.add(carrier)
    sess.commit()
    got = carrier.id
    sess.close()
    yield got


@pytest.fixture
def policy_with_odd_carrier(engine):
    """A policy whose free-text carrier name resolves to no carrier."""
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Acme Landscaping LLC")
    sess.add(client)
    sess.flush()
    policy = Policy(client_id=client.id,
                    carrier_name="Some Other Insurance Company",
                    policy_number="P-1", line_of_business="commercial_auto",
                    state="CA")
    sess.add(policy)
    sess.commit()
    got = policy.id
    sess.close()
    yield got


@pytest.fixture
def policy_id(policy_with_odd_carrier):
    yield policy_with_odd_carrier


def test_unresolved_carrier_names_are_listed(client_app, policy_with_odd_carrier):
    assert "Some Other Insurance Company" in client_app.get("/settings").text


def test_creating_a_carrier_from_the_settings_page(client_app, db):
    client_app.post("/settings/carriers",
                    data={"display_name": "Scottsdale Insurance Company"},
                    follow_redirects=False)
    assert db.query(Carrier).filter_by(
        display_name="Scottsdale Insurance Company").count() == 1


def test_adding_an_alias_resolves_the_odd_name(client_app, db, carrier_id):
    client_app.post(f"/settings/carriers/{carrier_id}/alias",
                    data={"alias": "Some Other Insurance Company"},
                    follow_redirects=False)
    assert resolve_carrier(db, "Some Other Insurance Company").id == carrier_id


def test_setting_admitted_status_records_the_state(client_app, db, carrier_id):
    client_app.post(f"/settings/carriers/{carrier_id}/admitted",
                    data={"state": "ca", "status": "non_admitted"},
                    follow_redirects=False)
    assert admitted_status(db, carrier_id, "CA") == "non_admitted"


def test_an_invalid_admitted_status_is_rejected(client_app, carrier_id):
    response = client_app.post(f"/settings/carriers/{carrier_id}/admitted",
                               data={"state": "CA", "status": "probably fine"},
                               follow_redirects=False)
    assert response.status_code == 422


def test_a_state_that_is_not_two_letters_is_rejected(client_app, carrier_id):
    response = client_app.post(f"/settings/carriers/{carrier_id}/admitted",
                               data={"state": "California", "status": "admitted"},
                               follow_redirects=False)
    assert response.status_code == 422


def test_a_duplicate_carrier_name_is_refused(client_app, carrier_id):
    response = client_app.post("/settings/carriers",
                               data={"display_name": "scottsdale insurance company"},
                               follow_redirects=False)
    assert response.status_code == 409


def test_setting_billing_type_appends(client_app, db, policy_id):
    client_app.post(f"/settings/policies/{policy_id}/billing-type",
                    data={"billing_type": "direct_bill"}, follow_redirects=False)
    client_app.post(f"/settings/policies/{policy_id}/billing-type",
                    data={"billing_type": "agency_bill"}, follow_redirects=False)
    rows = db.query(PolicyBillingType).filter_by(policy_id=policy_id).order_by(
        PolicyBillingType.id).all()
    assert [r.billing_type for r in rows] == ["direct_bill", "agency_bill"]


def test_an_invalid_billing_type_is_rejected(client_app, policy_id):
    response = client_app.post(f"/settings/policies/{policy_id}/billing-type",
                               data={"billing_type": "cash in an envelope"},
                               follow_redirects=False)
    assert response.status_code == 422


def test_aliasing_from_the_unresolved_list_picks_the_carrier_from_a_select(
    client_app, db, carrier_id, policy_with_odd_carrier
):
    """The path-based route cannot serve this control: a <select> cannot change
    a form's action without scripting."""
    response = client_app.post(
        "/settings/aliases",
        data={"carrier_id": str(carrier_id),
              "alias": "Some Other Insurance Company"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert resolve_carrier(db, "Some Other Insurance Company").id == carrier_id
