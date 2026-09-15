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
from tests.authhelp import sign_in


def _form(**overrides):
    """Every field on the preferences form, because a browser posts every one.

    save_preferences is deliberately all-or-nothing (a partial save leaves her
    guessing which fields took), so a hard-coded subset here is a test that
    breaks the next time a preference is added — and breaks by refusing the
    whole form, which looks nothing like the cause. Pass None to leave a
    checkbox unticked.
    """
    from renewal.settings_store import DEFINITIONS

    defaults = {
        "notify_enabled": "on",
        "notify_to": "her@agency.com",
        "notify_min_interval_minutes": "60",
        "digest_enabled": "on",
        "digest_hour": "8",
        "digest_quiet_days": "7",
        "attention_premium_pct": "10",
        "unconfirmed_date_window_days": "14",
        "session_ttl_hours": "12",
    }
    missing = {d.key for d in DEFINITIONS} - set(defaults)
    assert not missing, f"preference added with no value here: {missing}"
    return {k: v for k, v in (defaults | overrides).items() if v is not None}


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
        session_cookie_secure=False,
    )
    app = create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
    )
    with TestClient(app) as test_client:
        sign_in(test_client, engine)
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


def test_the_preferences_panel_shows_the_values_in_force(client_app):
    """Not her stored rows: an untouched setting has to show the value
    actually in force, not an empty box that looks unconfigured."""
    page = client_app.get("/settings").text
    assert "Preferences" in page
    assert 'name="attention_premium_pct"' in page
    assert 'value="10.0"' in page or 'value="10"' in page


def test_saving_a_preference_takes_effect(client_app, db):
    from renewal.settings_store import effective
    from tests.test_dates_llm import _settings

    response = client_app.post(
        "/settings/preferences",
        data=_form(notify_min_interval_minutes="30", attention_premium_pct="25",
                   unconfirmed_date_window_days="30", session_ttl_hours="8"),
        follow_redirects=False,
    )
    assert response.status_code == 303

    got = effective(db, _settings())
    assert got.attention_premium_pct == 25.0
    assert got.unconfirmed_date_window_days == 30
    assert got.notify_to == "her@agency.com"
    assert got.notify_enabled is True


def test_an_unticked_checkbox_turns_it_off(client_app, db):
    """A checkbox that is off is absent from the form rather than false."""
    from renewal.settings_store import effective
    from tests.test_dates_llm import _settings

    client_app.post(
        "/settings/preferences",
        data=_form(notify_enabled=None),
        follow_redirects=False,
    )
    assert effective(db, _settings()).notify_enabled is False


def test_a_refused_value_stores_nothing_at_all(client_app, db):
    """Partial saves are worse than none: she would be left guessing which
    fields took."""
    from renewal.models import AgencySetting

    response = client_app.post(
        "/settings/preferences",
        data=_form(attention_premium_pct="500"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert db.query(AgencySetting).count() == 0


def test_the_refusal_reaches_the_page(client_app):
    response = client_app.post(
        "/settings/preferences",
        data={"notify_to": "nonsense", "notify_min_interval_minutes": "60",
              "attention_premium_pct": "10",
              "unconfirmed_date_window_days": "14",
              "session_ttl_hours": "12"},
    )
    assert "not an email address" in response.text
