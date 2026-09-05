"""Login and logout.

Every failure says the same thing. Distinguishing "no such account" from
"wrong password" would turn the login form into an address oracle.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.auth.passwords import hash_password
from renewal.auth.sessions import COOKIE_NAME
from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import User, UserSession
from renewal.web import create_app

PASSWORD = "a-long-enough-password"
WRONG = "Email or password is wrong"


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
def app_client(engine, clean_db, settings):
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
def account(db):
    user = User(email="anne@agency.com", password_hash=hash_password(PASSWORD),
                display_name="Anne")
    db.add(user)
    db.commit()
    return user


def test_the_login_page_renders_without_a_cookie(app_client):
    response = app_client.get("/login")
    assert response.status_code == 200
    assert "password" in response.text.lower()


def test_a_correct_password_sets_a_session_cookie(app_client, account):
    response = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert app_client.cookies.get(COOKIE_NAME)


def test_the_cookie_is_httponly_and_samesite_lax(app_client, account):
    response = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
        follow_redirects=False,
    )
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


def test_the_address_is_matched_case_insensitively(app_client, account):
    response = app_client.post(
        "/login", data={"email": "Anne@Agency.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_a_wrong_password_sets_no_cookie(app_client, account):
    response = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": "wrong"},
    )
    assert response.status_code == 200
    assert WRONG in response.text
    assert not app_client.cookies.get(COOKIE_NAME)


def test_an_unknown_address_says_exactly_the_same_thing(app_client, account):
    response = app_client.post(
        "/login", data={"email": "nobody@agency.com", "password": PASSWORD},
    )
    assert response.status_code == 200
    assert WRONG in response.text


def test_a_deactivated_account_says_exactly_the_same_thing(
    app_client, account, db
):
    account.is_active = False
    db.commit()
    response = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
    )
    assert WRONG in response.text
    assert not app_client.cookies.get(COOKIE_NAME)


def test_logout_deletes_the_session_row(app_client, account, db):
    app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
    )
    assert db.query(UserSession).count() == 1
    app_client.post("/logout", follow_redirects=False)
    assert db.query(UserSession).count() == 0


def test_a_stale_hash_is_upgraded_on_a_successful_login(app_client, db):
    """The only moment the plaintext exists is a successful login."""
    from renewal.auth import passwords

    salt = b"0123456789abcdef"
    cheap = "$".join([
        "scrypt", str(2 ** 14), "8", "1", passwords._b64(salt),
        passwords._b64(passwords._derive(PASSWORD, salt, 2 ** 14, 8, 1)),
    ])
    db.add(User(email="anne@agency.com", password_hash=cheap,
                display_name="Anne"))
    db.commit()

    app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
    )
    db.expire_all()
    assert not passwords.needs_rehash(
        db.query(User).one().password_hash
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/calendar", "/calendar"),
        ("/clients/3?tab=docs", "/clients/3?tab=docs"),
        ("https://evil.example/x", "/"),
        ("//evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_next_is_confined_to_this_site(raw, expected):
    """An open redirect on the calendar was closed once already (298ac84).
    A login form is the classic place to reopen it."""
    from renewal.web.auth import safe_next

    assert safe_next(raw) == expected


def test_a_login_honours_a_safe_next(app_client, account):
    response = app_client.post(
        "/login?next=/calendar",
        data={"email": "anne@agency.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/calendar"
