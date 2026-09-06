"""Sign a TestClient in.

Every web test that is not specifically about the gate wants an authenticated
client. This puts the account creation in one place so that a change to the
login form is one edit rather than nine.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from renewal.auth.passwords import hash_password
from renewal.models import User

PASSWORD = "a-long-enough-password"
EMAIL = "tests@agency.com"


def sign_in(test_client, engine) -> None:
    session = sessionmaker(bind=engine)()
    try:
        if session.query(User).filter_by(email=EMAIL).first() is None:
            session.add(User(email=EMAIL, password_hash=hash_password(PASSWORD),
                             display_name="Test"))
            session.commit()
    finally:
        session.close()
    response = test_client.post(
        "/login", data={"email": EMAIL, "password": PASSWORD}
    )
    assert response.status_code in (200, 303), response.status_code
    # Checked by asking the gate rather than by looking at the cookie jar. A
    # Secure cookie is stored but never *sent* over the TestClient's http://
    # base URL, so a fixture that forgets session_cookie_secure=False looks
    # signed in here and is refused on every later request.
    landing = test_client.get("/", follow_redirects=False)
    assert landing.status_code != 303, (
        "signed in but the gate still refuses: is session_cookie_secure "
        "left at its default of True in this fixture's Settings?"
    )
