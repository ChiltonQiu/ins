"""The default-deny gate.

Everything requires a session except five paths. Those five are listed here as
well as in the middleware, so removing one from the allowlist without meaning
to fails a test that says why it is public.
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.auth.passwords import hash_password
from renewal.auth.sessions import COOKIE_NAME
from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Agency, User, UserSession
from renewal.web import create_app

PASSWORD = "a-long-enough-password"


class Refusing:
    """A provider that authenticates nothing successfully.

    The default file-drop provider verifies everything, so the webhook would
    answer 200 and the test could not tell the route's answer from the gate's.
    A refusing provider makes the route's own 403 the thing under test.
    """

    def verify(self, headers, body):
        return False

    def parse(self, headers, body):
        raise AssertionError("parse must not run when verification fails")


@pytest.fixture
def bare_client(engine, clean_db, tmp_path):
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
        inbound_provider=Refusing(),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(engine):
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture
def signed_in(bare_client, db):
    db.add(User(email="anne@agency.com",
                password_hash=hash_password(PASSWORD), display_name="Anne"))
    db.commit()
    bare_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
    )
    return bare_client


@pytest.mark.parametrize(
    "path", ["/", "/calendar", "/search", "/clients", "/attention",
             "/unmatched", "/settings", "/runs/new"],
)
def test_a_protected_page_redirects_to_login(bare_client, path):
    response = bare_client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={path}"


def test_the_redirect_carries_the_query_string(bare_client):
    response = bare_client.get("/search?q=acme", follow_redirects=False)
    assert response.headers["location"] == "/login?next=/search%3Fq%3Dacme"


def test_a_protected_post_is_403_not_a_redirect(bare_client):
    """A form post that silently became a login page looks to the operator
    like the action succeeded."""
    response = bare_client.post(
        "/attention/1/done", follow_redirects=False
    )
    assert response.status_code == 403


def test_a_signed_in_request_gets_through(signed_in):
    assert signed_in.get("/").status_code == 200


def test_a_revoked_session_stops_working(signed_in, db):
    db.query(UserSession).delete()
    db.commit()
    response = signed_in.get("/", follow_redirects=False)
    assert response.status_code == 303


def test_an_expired_session_stops_working(signed_in, db):
    from datetime import datetime, timedelta, timezone

    row = db.query(UserSession).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    response = signed_in.get("/", follow_redirects=False)
    assert response.status_code == 303


def test_a_forged_cookie_does_not_work(bare_client):
    bare_client.cookies.set(COOKIE_NAME, "not-a-real-token")
    response = bare_client.get("/", follow_redirects=False)
    assert response.status_code == 303


# ---- the five public paths ------------------------------------------------

def test_login_is_public(bare_client):
    assert bare_client.get("/login").status_code == 200


def test_logout_is_public(bare_client):
    """Reachable with a dead session, so the way out is never barred by the
    gate someone is already outside of. It clears the cookie and lands on the
    login page."""
    response = bare_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_static_is_public(bare_client):
    assert bare_client.get("/static/app.css").status_code == 200


def test_the_ics_feed_is_public(bare_client, db):
    """A calendar client cannot log in. The URL carries its own token."""
    agency = db.query(Agency).one()
    response = bare_client.get(f"/calendar/{agency.ics_token}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")


def test_a_wrong_ics_token_is_still_404_not_a_redirect(bare_client):
    """The route stays public, so a bad token gets the route's own answer."""
    response = bare_client.get("/calendar/nope.ics", follow_redirects=False)
    assert response.status_code == 404


def test_the_mail_webhook_is_public(bare_client):
    """Reached with no cookie and no Origin. It gets the route's own 403 for
    failed signature verification, not the gate's."""
    response = bare_client.post("/inbound/mail", content=b"x")
    assert response.status_code == 403
    assert "not verified" in response.text


# ---- cross-site posts -----------------------------------------------------

def test_a_cross_site_post_is_rejected(signed_in):
    response = signed_in.post(
        "/attention/1/done", headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_a_same_origin_post_is_allowed(signed_in):
    """Not a 403 from the gate. Whatever the route itself answers is fine.

    A route that needs no body and no existing row, so the only thing this can
    fail on is the gate.
    """
    response = signed_in.post(
        "/settings/regenerate-ics-token",
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code != 403


def test_a_post_with_no_origin_is_allowed(signed_in):
    """curl and the app's own fetch() calls send none. SameSite=Lax is what
    stops a cross-site form post; the Origin check is the second lock."""
    response = signed_in.post(
        "/settings/regenerate-ics-token", follow_redirects=False
    )
    assert response.status_code != 403


def test_a_get_with_a_foreign_origin_is_fine(signed_in):
    """The check is on state-changing methods only."""
    response = signed_in.get("/", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


# ---- the topbar -----------------------------------------------------------

def test_the_topbar_shows_who_is_signed_in(signed_in):
    body = signed_in.get("/").text
    assert "anne@agency.com" in body
    assert re.search(r'action="/logout"', body)
