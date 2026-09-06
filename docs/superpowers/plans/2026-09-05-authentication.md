# Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the whole web application behind named user accounts, so the book of business is no longer readable by anyone who can reach the port.

**Architecture:** A `renewal/auth/` package of pure functions over a SQLAlchemy `Session` (password hashing, session lifecycle), a thin `renewal/web/auth.py` holding login and logout, and one default-deny middleware in `create_app` that requires a session for every request except five public paths. Sessions are opaque random tokens in an `HttpOnly` cookie; only their SHA-256 is stored, in a `user_session` table, so logout and revocation actually work.

**Tech Stack:** FastAPI, Starlette middleware, SQLAlchemy 2.0 `Mapped`/`mapped_column`, Alembic, Jinja2, Postgres, pytest + `TestClient`. Password hashing is `hashlib.scrypt` from the standard library — no new dependency.

**Spec:** `docs/superpowers/specs/2026-09-05-authentication-design.md`

## Global Constraints

- **No new runtime dependencies.** Password hashing uses `hashlib.scrypt` from the standard library. Do not add `passlib`, `bcrypt`, `argon2-cffi`, or `itsdangerous`.
- **`hashlib.scrypt` needs an explicit `maxmem`.** At `n=2**15, r=8` the derivation needs 32 MiB and OpenSSL's default limit rejects it with `[digital envelope routines] memory limit exceeded`. Always pass `maxmem=64 * 1024 * 1024`. This is verified: without it the call raises, with it it takes ~150 ms.
- **Tables are `app_user` and `user_session`.** Not `user` (reserved word in Postgres) and not `session`. Model classes are `User` and `UserSession` — a class named `Session` would shadow the SQLAlchemy `Session` that the web modules import.
- **The public allowlist is exactly five entries** and nothing may be added to it in this plan: `/login`, `/logout`, `/static/*`, `/calendar/{token}.ics`, `/inbound/mail`. `/logout` is public so that clicking it with an already-dead session clears the cookie rather than being refused by the gate it is trying to leave.
- **Every login failure returns the same message:** `Email or password is wrong.` Wrong password, unknown address, inactive account, and locked account are indistinguishable to the caller.
- **`SESSION_COOKIE_SECURE` defaults to `true`.** Forgetting to configure it must fail toward security.
- **Insert-only is not a rule here.** `renewal/models.py` opens by saying application code never issues UPDATE or DELETE. That rule is about the *record* — extractions, corrections, promotions — where history is the point. Credentials are state, not record: logout deletes a session row and a lockout counter is updated in place. Task 2 adds a note to the models docstring saying so, rather than leaving the two in silent contradiction.
- **Do not touch** `Correction`, `DateEvent`, `AttentionEvent`, `Reclassification`, `ManualDateEvent`, or the promotion path. Attribution is a separate change.

---

### Task 1: Password hashing

**Files:**
- Create: `renewal/auth/__init__.py`
- Create: `renewal/auth/passwords.py`
- Create: `tests/test_auth_passwords.py`
- Modify: `pyproject.toml` — add `"renewal.auth"` to `[tool.setuptools] packages`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `hash_password(password: str) -> str`
  - `verify_password(password: str, encoded: str) -> bool`
  - `needs_rehash(encoded: str) -> bool`
  - `DUMMY_HASH: str` — a module-level encoded hash of an unguessable value, for equalizing timing when no account exists.

`tests/test_packaging.py` already derives the on-disk package set by globbing for `__init__.py`, so creating `renewal/auth/` makes it fail until `pyproject.toml` is updated. That is the test doing its job — it is why the pyproject edit belongs in this task rather than a later one.

- [x] **Step 1: Write the failing test**

Create `tests/test_auth_passwords.py`:

```python
"""Password hashing.

scrypt rather than a KDF from a dependency: the project's dependency list is
deliberately short, and the standard library's is memory-hard.
"""

import pytest

from renewal.auth.passwords import (
    DUMMY_HASH, hash_password, needs_rehash, verify_password,
)


def test_a_password_verifies_against_its_own_hash():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)


def test_a_wrong_password_does_not_verify():
    encoded = hash_password("correct horse battery staple")
    assert not verify_password("Correct horse battery staple", encoded)


def test_two_hashes_of_one_password_differ():
    """Salted. Equal hashes would mean two accounts with the same password
    are visibly the same account in a database dump."""
    assert hash_password("hunter2 hunter2") != hash_password("hunter2 hunter2")


def test_the_encoded_form_carries_its_parameters():
    encoded = hash_password("hunter2 hunter2")
    scheme, n, r, p, salt, digest = encoded.split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (2 ** 15, 8, 1)
    assert salt and digest


def test_a_hash_at_current_parameters_does_not_need_rehashing():
    assert not needs_rehash(hash_password("hunter2 hunter2"))


def test_a_cheaper_hash_needs_rehashing():
    """Cost gets raised over time. An old hash must still verify, and must be
    replaced the next time the password is available in plaintext."""
    encoded = hash_password("hunter2 hunter2")
    scheme, n, r, p, salt, digest = encoded.split("$")
    cheaper = "$".join([scheme, str(2 ** 14), r, p, salt, digest])
    assert needs_rehash(cheaper)


def test_a_cheaper_hash_still_verifies():
    from renewal.auth import passwords

    salt = b"0123456789abcdef"
    dk = passwords._derive("hunter2 hunter2", salt, 2 ** 14, 8, 1)
    cheaper = "$".join(
        ["scrypt", str(2 ** 14), "8", "1", passwords._b64(salt),
         passwords._b64(dk)]
    )
    assert verify_password("hunter2 hunter2", cheaper)


@pytest.mark.parametrize(
    "encoded",
    ["", "not-a-hash", "scrypt$x$8$1$aaaa$bbbb", "scrypt$32768$8$1$aaaa",
     "bcrypt$32768$8$1$aaaa$bbbb"],
)
def test_a_malformed_hash_is_false_not_an_exception(encoded):
    """A corrupt row must fail the login, not 500 the login page."""
    assert not verify_password("hunter2 hunter2", encoded)


def test_the_dummy_hash_verifies_against_nothing():
    """It exists to burn the same time as a real verification when no account
    matches, so response timing does not disclose which addresses exist."""
    assert not verify_password("hunter2 hunter2", DUMMY_HASH)
    assert not needs_rehash(DUMMY_HASH)
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_auth_passwords.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.auth'`

- [x] **Step 3: Write the implementation**

Create `renewal/auth/__init__.py` as an empty file.

Create `renewal/auth/passwords.py`:

```python
"""Password hashing.

scrypt from the standard library rather than a KDF from a dependency: it is
memory-hard, and this project's dependency list is deliberately short.

The encoded form carries its own parameters, so cost can be raised later
without invalidating every existing hash. A hash that verifies under old
parameters is rewritten at the next login, which is the only moment the
plaintext is available.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
_SALT_BYTES = 16
_DKLEN = 32

# scrypt at n=2**15, r=8 needs 32 MiB, and OpenSSL's default ceiling rejects
# exactly that with "memory limit exceeded". The limit has to be raised
# explicitly or the call never succeeds.
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=_DKLEN, maxmem=_MAXMEM,
    )


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    digest = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join([
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        _b64(salt), _b64(digest),
    ])


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """None for anything that is not a hash this module wrote. A corrupt row
    must fail the login rather than raise out of it."""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
    except ValueError:
        return None
    if scheme != "scrypt":
        return None
    try:
        return int(n), int(r), int(p), _unb64(salt), _unb64(digest)
    except (ValueError, TypeError):
        return None


def verify_password(password: str, encoded: str) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, digest = parsed
    try:
        candidate = _derive(password, salt, n, r, p)
    except ValueError:
        # Parameters outside what scrypt accepts, e.g. an n that is not a
        # power of two. Same answer as a wrong password.
        return False
    return hmac.compare_digest(candidate, digest)


def needs_rehash(encoded: str) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, _, _ = parsed
    return (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


# Verified against when no account matches, so an unknown address costs the
# same wall-clock time as a wrong password. Nobody knows this plaintext.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
```

- [x] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_auth_passwords.py -v`
Expected: PASS, all cases.

- [x] **Step 5: Confirm the packaging test now fails, then fix it**

Run: `.venv/bin/pytest tests/test_packaging.py -v`
Expected: FAIL — `not in pyproject packages: ['renewal.auth']`

In `pyproject.toml`, add `"renewal.auth"` to the `[tool.setuptools] packages` list, keeping the existing alphabetical order — it goes first, before `"renewal.attention"`:

```toml
packages = [
  "renewal", "renewal.attention", "renewal.auth", "renewal.calendarview",
  "renewal.classify", "renewal.clients", "renewal.dates", "renewal.extract",
  "renewal.mail", "renewal.resolve", "renewal.search", "renewal.text",
  "renewal.web",
]
```

- [x] **Step 6: Run the packaging test to verify it passes**

Run: `.venv/bin/pytest tests/test_packaging.py -v`
Expected: PASS

- [x] **Step 7: Commit**

```bash
git add renewal/auth/__init__.py renewal/auth/passwords.py \
        tests/test_auth_passwords.py pyproject.toml
git commit -m "feat(auth): scrypt password hashing with upgradable parameters"
```

---

### Task 2: The user and session tables

**Files:**
- Modify: `renewal/models.py` — module docstring, and two new classes at the end
- Create: `migrations/versions/<generated>_users_and_sessions.py`
- Modify: `conftest.py:47-56` — the `TABLES` tuple
- Test: `tests/test_models_auth.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `renewal.models.User` — `id, email, password_hash, display_name, is_active, failed_count, locked_until, created_at`
  - `renewal.models.UserSession` — `id, token_sha256, user_id, created_at, expires_at, last_seen_at`

- [x] **Step 1: Write the failing test**

Create `tests/test_models_auth.py`:

```python
"""The two credential tables.

Unlike the record tables these are state, not history: a session row is
deleted at logout and a lockout counter is updated in place.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import User, UserSession


def _user(session, email="anne@agency.com"):
    user = User(email=email, password_hash="scrypt$x", display_name="Anne")
    session.add(user)
    session.flush()
    return user


def test_a_user_defaults_to_active_and_unlocked(session):
    user = _user(session)
    assert user.is_active is True
    assert user.failed_count == 0
    assert user.locked_until is None


def test_two_users_cannot_share_an_address_in_different_cases(session):
    """Uniqueness is on lower(email). Anne@ and anne@ are one person, and two
    rows would mean a login that succeeds or fails depending on shift key."""
    _user(session, "anne@agency.com")
    # Added without flushing, so the IntegrityError is raised by this flush
    # rather than inside the helper, outside the assertion.
    session.add(User(email="Anne@Agency.com", password_hash="scrypt$x",
                     display_name="Anne"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_session_hangs_off_a_user(session):
    user = _user(session)
    now = datetime.now(timezone.utc)
    session.add(UserSession(
        token_sha256="a" * 64, user_id=user.id,
        expires_at=now + timedelta(hours=12), last_seen_at=now,
    ))
    session.flush()
    assert session.query(UserSession).one().user_id == user.id


def test_two_sessions_cannot_share_a_token(session):
    user = _user(session)
    now = datetime.now(timezone.utc)
    for _ in range(2):
        session.add(UserSession(
            token_sha256="a" * 64, user_id=user.id,
            expires_at=now + timedelta(hours=12), last_seen_at=now,
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_a_user_takes_their_sessions_with_them(session):
    user = _user(session)
    now = datetime.now(timezone.utc)
    session.add(UserSession(
        token_sha256="b" * 64, user_id=user.id,
        expires_at=now + timedelta(hours=12), last_seen_at=now,
    ))
    session.flush()
    session.delete(user)
    session.flush()
    assert session.query(UserSession).count() == 0
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_models_auth.py -v`
Expected: FAIL — `ImportError: cannot import name 'User' from 'renewal.models'`

- [x] **Step 3: Add the models**

In `renewal/models.py`, extend the module docstring. It currently opens by
saying every table is insert-only; that must not silently stop being true:

```python
"""Every table here is insert-only. Application code never issues UPDATE or
DELETE: corrections, re-extractions, re-promotions, and draft edits all insert
new rows. Values extracted from documents are stored as text exactly as read;
typed parsing happens in the diff layer.

The two exceptions are `app_user` and `user_session`, at the bottom. Those
hold credentials rather than record: a session is deleted at logout, and a
lockout counter is updated in place. Keeping a history of session rows would
be a liability, not an audit trail.
"""
```

Append at the end of the file. `Boolean`, `Integer`, `Index`, `ForeignKey`,
`Text`, `DateTime` and `func` are all already imported at the top; nothing new
is needed there.

```python
class User(Base):
    """A person who can sign in. Every account can do everything; there are no
    roles. `is_active` turns an account off without deleting the row, so a
    later change that attributes decisions to a user still has something to
    point at."""

    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class UserSession(Base):
    """Only the SHA-256 of the cookie value is stored, so a stolen database
    dump yields no usable session. Named UserSession rather than Session: the
    web modules all import SQLAlchemy's Session."""

    __tablename__ = "user_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_sha256: Mapped[str] = mapped_column(Text, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Uniqueness on lower(email) rather than on the column, so Anne@ and anne@
# cannot both exist. A functional index cannot be written inside
# __table_args__ without naming a column that does not exist until the class
# body has run, so it is declared here instead.
Index(
    "uq_app_user_email_lower",
    func.lower(User.__table__.c.email),
    unique=True,
)
```

- [x] **Step 4: Generate and edit the migration**

Run: `.venv/bin/alembic revision --autogenerate -m "users and sessions"`

Open the generated file. Autogenerate will produce both `create_table` calls
and the index. Verify it contains:

- `op.create_table('app_user', ...)` with `sa.PrimaryKeyConstraint('id')`
- `op.create_index('uq_app_user_email_lower', 'app_user', [sa.text('lower(email)')], unique=True)`
- `op.create_table('user_session', ...)` with
  `sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE')`
- `op.create_index(op.f('ix_user_session_token_sha256'), 'user_session', ['token_sha256'], unique=True)`

Add any of those it missed by hand — autogenerate does not reliably emit a
functional index. Confirm `down_revision` points at `2d63899b782b`.

- [x] **Step 5: Add the new tables to the test truncation list**

In `conftest.py`, the `TABLES` string ends with `"inbound_message, attention_item, attention_event"`. Extend it:

```python
TABLES = (
    "client, policy, policy_term, coverage, insured_item, document, extraction,"
    " extracted_field, correction, renewal_run, comparison, difference,"
    " reclassification, draft, carrier, carrier_alias, carrier_admitted_status,"
    " policy_billing_type, document_text, document_classification,"
    " document_link, document_date, date_event, manual_date, manual_date_event,"
    " inbound_message, attention_item, attention_event, app_user, user_session"
)
```

`user_session` does not need naming separately — `TRUNCATE ... CASCADE` on
`app_user` reaches it — but naming it is clearer and costs nothing.

- [x] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_models_auth.py tests/test_models.py -v`
Expected: PASS. The `engine` fixture drops and recreates the schema from
`alembic upgrade head`, so the new migration runs as part of the test session.

- [x] **Step 7: Run the full suite to confirm nothing regressed**

Run: `.venv/bin/pytest`
Expected: PASS — 510 existing tests plus the new ones.

- [x] **Step 8: Commit**

```bash
git add renewal/models.py migrations/versions conftest.py \
        tests/test_models_auth.py
git commit -m "feat(auth): app_user and user_session tables"
```

---

### Task 3: Session lifecycle

**Files:**
- Create: `renewal/auth/sessions.py`
- Test: `tests/test_auth_sessions.py`

**Interfaces:**
- Consumes: `renewal.models.User`, `renewal.models.UserSession` from Task 2.
- Produces:
  - `create_session(session, user, *, ttl_hours: int) -> str` — returns the raw cookie value, which is never stored and never recoverable afterwards
  - `lookup_session(session, token: str, *, ttl_hours: int) -> User | None` — refuses expired and inactive; on success slides `last_seen_at` to now and `expires_at` to `now + ttl_hours` (a flat window; deriving the span from the mutated `expires_at` compounds it)
  - `revoke_session(session, token: str) -> None`
  - `sweep_expired(session) -> int`
  - `COOKIE_NAME: str = "renewal_session"`

- [x] **Step 1: Write the failing test**

Create `tests/test_auth_sessions.py`:

```python
"""Session lifecycle.

The table exists so that logout revokes. A signed cookie would be cheaper and
would leave a captured token valid until it expired.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from renewal.auth.sessions import (
    create_session, lookup_session, revoke_session, sweep_expired,
)
from renewal.models import User, UserSession


def _user(session, email="anne@agency.com", active=True):
    user = User(email=email, password_hash="scrypt$x", display_name="Anne",
                is_active=active)
    session.add(user)
    session.flush()
    return user


def test_a_new_session_looks_up_to_its_user(session):
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    assert lookup_session(session, token).id == user.id


def test_the_raw_token_is_not_stored(session):
    """Only its digest is. A database dump must not hand over live cookies."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    row = session.query(UserSession).one()
    assert token not in row.token_sha256
    assert row.token_sha256 == hashlib.sha256(token.encode()).hexdigest()


def test_two_sessions_get_different_tokens(session):
    user = _user(session)
    assert create_session(session, user, ttl_hours=12) != create_session(
        session, user, ttl_hours=12
    )


def test_an_unknown_token_is_none(session):
    assert lookup_session(session, "nonsense") is None


def test_an_expired_session_is_none(session):
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    row = session.query(UserSession).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()
    assert lookup_session(session, token) is None


def test_a_session_belonging_to_a_deactivated_user_is_none(session):
    """Turning an account off must end the sessions it already has, not only
    stop it logging in again."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    user.is_active = False
    session.flush()
    assert lookup_session(session, token) is None


def test_lookup_slides_the_expiry(session):
    """An active session must not expire in the middle of a long review."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    row = session.query(UserSession).one()
    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.flush()
    before = row.expires_at
    lookup_session(session, token)
    assert row.expires_at > before


def test_revoking_makes_the_token_dead(session):
    """Logout deletes the row. Clearing the cookie alone would leave a
    captured token working."""
    user = _user(session)
    token = create_session(session, user, ttl_hours=12)
    revoke_session(session, token)
    assert lookup_session(session, token) is None
    assert session.query(UserSession).count() == 0


def test_revoking_an_unknown_token_is_quiet(session):
    revoke_session(session, "nonsense")


def test_the_sweep_removes_only_expired_rows(session):
    user = _user(session)
    live = create_session(session, user, ttl_hours=12)
    dead = create_session(session, user, ttl_hours=12)
    dead_digest = hashlib.sha256(dead.encode()).hexdigest()
    row = session.query(UserSession).filter_by(token_sha256=dead_digest).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()
    assert sweep_expired(session) == 1
    assert lookup_session(session, live).id == user.id
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_auth_sessions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.auth.sessions'`

- [x] **Step 3: Write the implementation**

Create `renewal/auth/sessions.py`:

```python
"""Session lifecycle.

The cookie carries a random opaque token; the table stores only its SHA-256.
That is what makes logout mean something — the row is deleted and the token
stops working — and it means a stolen database dump contains no live
credentials.

There is no scheduled sweep. An expired row is already refused by lookup, so
removing it is housekeeping and can ride along with the next login.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from renewal.models import User, UserSession

COOKIE_NAME = "renewal_session"
_TOKEN_BYTES = 32


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(session: Session, user: User, *, ttl_hours: int) -> str:
    """Returns the raw cookie value. It is not stored and cannot be recovered
    afterwards — this return value is the only time it exists."""
    sweep_expired(session)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = datetime.now(timezone.utc)
    session.add(UserSession(
        token_sha256=_digest(token),
        user_id=user.id,
        # Set explicitly rather than left to the server default: lookup slides
        # the expiry by (expires_at - created_at), and that arithmetic must
        # not depend on whether the default has been read back yet.
        created_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
        last_seen_at=now,
    ))
    session.flush()
    return token


def lookup_session(session: Session, token: str, *, ttl_hours: int) -> User | None:
    row = session.scalar(
        select(UserSession).where(UserSession.token_sha256 == _digest(token))
    )
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if row.expires_at <= now:
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    # Slide, so a session in use does not expire mid-task. The window is
    # flat: each use grants another full TTL from now. Deriving the span from
    # the row's own expires_at instead compounds it, because this function has
    # already moved that value on every prior hit.
    row.expires_at = now + timedelta(hours=ttl_hours)
    row.last_seen_at = now
    session.flush()
    return user


def revoke_session(session: Session, token: str) -> None:
    session.execute(
        delete(UserSession).where(UserSession.token_sha256 == _digest(token))
    )
    session.flush()


def sweep_expired(session: Session) -> int:
    result = session.execute(
        delete(UserSession).where(
            UserSession.expires_at <= datetime.now(timezone.utc)
        )
    )
    session.flush()
    return result.rowcount
```

- [x] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_auth_sessions.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add renewal/auth/sessions.py tests/test_auth_sessions.py
git commit -m "feat(auth): db-backed sessions that logout actually revokes"
```

---

### Task 4: Login and logout

**Files:**
- Create: `renewal/web/auth.py`
- Create: `renewal/templates/login.html`
- Modify: `renewal/config.py` — two settings and their `load_settings` entries
- Modify: `.env.example` — document them
- Modify: `renewal/web/__init__.py` — register the auth router
- Test: `tests/test_web_auth.py`

**Interfaces:**
- Consumes: `hash_password`, `verify_password`, `needs_rehash`, `DUMMY_HASH` (Task 1); `create_session`, `lookup_session`, `revoke_session`, `COOKIE_NAME` (Task 3); `renewal.models.User` (Task 2).
- Produces:
  - `renewal.web.auth.register(app, deps) -> None`
  - `renewal.web.auth.safe_next(raw: str | None) -> str` — used by Task 7's middleware
  - `Settings.session_cookie_secure: bool`, `Settings.session_ttl_hours: int`

Nothing is enforced yet. This task adds the door; Task 7 closes the walls.

- [x] **Step 1: Add the settings**

In `renewal/config.py`, add to the `Settings` dataclass after `inbound_drop_dir`:

```python
    session_cookie_secure: bool = True
    session_ttl_hours: int = 12
```

and in `load_settings()`, after the `inbound_drop_dir` entry:

```python
        # Defaults to true so that forgetting to configure it fails toward
        # security. Local development over plain HTTP sets it false.
        session_cookie_secure=os.environ.get(
            "SESSION_COOKIE_SECURE", "true"
        ).lower() not in ("0", "false", "no"),
        session_ttl_hours=int(os.environ.get("SESSION_TTL_HOURS", "12")),
```

Append to `.env.example`:

```
# Authentication. Accounts are created from the command line only:
#   python -m scripts.add_user anne@agency.com
# There is no signup route and no password-reset email; re-running the script
# for an existing address resets that password.

# Set false ONLY for local development over plain HTTP. Left true, the session
# cookie is never sent over an unencrypted connection.
SESSION_COOKIE_SECURE=true

# About one working day, so she logs in each morning rather than mid-task.
# An active session slides, so a long review does not expire underneath her.
SESSION_TTL_HOURS=12
```

- [x] **Step 2: Write the failing test**

Create `tests/test_web_auth.py`:

```python
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
```

- [x] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_web_auth.py -v`
Expected: FAIL — `404` on `/login`, and `ModuleNotFoundError` on
`renewal.web.auth`.

- [x] **Step 4: Write the router**

Create `renewal/web/auth.py`:

```python
"""Login and logout.

Every failure returns one message. Telling a caller that an address exists but
the password was wrong turns this form into a way to enumerate the staff, and
the value of that distinction to the person signing in is nil.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from renewal.auth.passwords import (
    DUMMY_HASH, hash_password, needs_rehash, verify_password,
)
from renewal.auth.sessions import COOKIE_NAME, create_session, revoke_session
from renewal.models import User
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

logger = logging.getLogger(__name__)

WRONG = "Email or password is wrong."


def safe_next(raw: str | None) -> str:
    """Confine a redirect to this site.

    Anything that is not a single-slash-prefixed relative path becomes "/".
    That rejects an absolute URL, a protocol-relative "//host" and the
    backslash variants some browsers normalise into one.
    """
    if not raw or not raw.startswith("/"):
        return "/"
    if raw.startswith("//") or raw.startswith("/\\"):
        return "/"
    return raw


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    settings = deps.settings
    router = APIRouter()

    def _page(request: Request, error: str | None, status: int = 200):
        return TEMPLATES.TemplateResponse(
            request, "login.html",
            {"error": error, "next": safe_next(
                request.query_params.get("next")
            )},
            status_code=status,
        )

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return _page(request, error=None)

    @router.post("/login")
    def login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
    ):
        destination = safe_next(request.query_params.get("next"))
        with session_factory() as session:
            user = session.scalar(
                select(User).where(
                    func.lower(User.email) == email.strip().lower()
                )
            )
            # Hash even when there is no account, so an unknown address costs
            # the same time as a wrong password.
            ok = verify_password(
                password, user.password_hash if user else DUMMY_HASH
            )
            if user is None or not user.is_active or not ok:
                session.commit()
                logger.info("login rejected email=%s", email.strip().lower())
                return _page(request, error=WRONG, status=200)

            user.failed_count = 0
            user.locked_until = None
            if needs_rehash(user.password_hash):
                user.password_hash = hash_password(password)
            token = create_session(
                session, user, ttl_hours=settings.session_ttl_hours
            )
            session.commit()

        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            COOKIE_NAME, token,
            httponly=True, samesite="lax",
            secure=settings.session_cookie_secure,
            path="/", max_age=settings.session_ttl_hours * 3600,
        )
        return response

    @router.post("/logout")
    def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            with session_factory() as session:
                revoke_session(session, token)
                session.commit()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    app.include_router(router)
```

There is no lockout here yet, and `locked_until` is not read. Task 5 adds
both, as a change that is reviewable on its own rather than dead code arriving
early. `timedelta` is imported above in anticipation of that task; if a linter
objects to it being unused, drop it here and add it back in Task 5.

- [x] **Step 5: Write the template**

Create `renewal/templates/login.html`. It does not extend `base.html`: none of
that navigation is reachable from here.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in — Renewal compare</title>
  <meta name="robots" content="noindex">
  <link rel="stylesheet" href="/static/app.css">
</head>
<body class="loginpage">
  <main class="loginbox">
    <h1>Renewal <span>compare</span></h1>
    {% if error %}
    <p class="loginerror" role="alert">{{ error }}</p>
    {% endif %}
    <form method="post" action="/login?next={{ next|urlencode }}">
      <div class="formrow">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="username"
               required autofocus>
      </div>
      <div class="formrow">
        <label for="password">Password</label>
        <input id="password" name="password" type="password"
               autocomplete="current-password" required>
      </div>
      <button type="submit" class="primary">Sign in</button>
    </form>
  </main>
</body>
</html>
```

Append to `renewal/static/app.css`:

```css
/* ---- login ------------------------------------------------------------- */
/* The one page with no navigation: nothing behind it is reachable yet. */
.loginbox {
  max-width: 22rem;
  margin: 12vh auto;
  padding: 0 1rem;
}
.loginbox h1 { margin-bottom: 1.5rem; }
.loginerror {
  border-left: 3px solid var(--bad);
  padding: 0.5rem 0.75rem;
  margin-bottom: 1rem;
}
```

If `--bad` is not the name of the existing red token, use whichever token the
comparison screen uses for a materially worse value — check `app.css` rather
than inventing a colour.

- [x] **Step 6: Register the router**

In `renewal/web/__init__.py`, import the module and add it to the tuple:

```python
from renewal.web import (
    attention as attention_routes, auth as auth_routes, calendar,
    clients as client_routes, comparison, mail as mail_routes, review, runs,
    search as search_routes, settings as settings_routes, unmatched,
)

ROUTER_MODULES = (
    runs, review, comparison, unmatched, calendar, settings_routes,
    search_routes, client_routes, attention_routes, auth_routes,
)
```

- [x] **Step 7: Run the tests**

Run: `.venv/bin/pytest tests/test_web_auth.py -v`
Expected: PASS for every test in the file.

- [x] **Step 8: Run the full suite**

Run: `.venv/bin/pytest`
Expected: PASS. Nothing is enforced yet, so no existing web test changes.

- [x] **Step 9: Commit**

```bash
git add renewal/web/auth.py renewal/templates/login.html \
        renewal/static/app.css renewal/config.py .env.example \
        renewal/web/__init__.py tests/test_web_auth.py
git commit -m "feat(auth): login and logout, with one message for every failure"
```

---

### Task 5: Lockout after repeated failures

**Files:**
- Modify: `renewal/web/auth.py` — add `_record_failure`, restore its call
- Test: `tests/test_web_auth.py` — append

**Interfaces:**
- Consumes: everything from Task 4.
- Produces: `MAX_FAILURES = 10`, `LOCKOUT_MINUTES = 15` in `renewal/web/auth.py`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_web_auth.py`:

```python
def _fail_once(client, email="anne@agency.com"):
    return client.post("/login", data={"email": email, "password": "wrong"})


def test_ten_failures_lock_the_account(app_client, account, db):
    for _ in range(10):
        _fail_once(app_client)
    db.expire_all()
    assert db.query(User).one().locked_until is not None


def test_the_correct_password_fails_while_locked(app_client, account, db):
    for _ in range(10):
        _fail_once(app_client)
    response = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
    )
    assert WRONG in response.text
    assert not app_client.cookies.get(COOKIE_NAME)


def test_the_lock_says_nothing_different(app_client, account):
    """A distinct 'account locked' message tells an attacker the address is
    real and that they are making progress."""
    for _ in range(10):
        _fail_once(app_client)
    locked = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": "wrong"},
    )
    unknown = app_client.post(
        "/login", data={"email": "nobody@agency.com", "password": "wrong"},
    )
    assert WRONG in locked.text
    assert WRONG in unknown.text


def test_nine_failures_do_not_lock(app_client, account):
    for _ in range(9):
        _fail_once(app_client)
    response = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_an_expired_lock_lets_a_correct_password_through(
    app_client, account, db
):
    from datetime import datetime, timedelta, timezone

    for _ in range(10):
        _fail_once(app_client)
    db.expire_all()
    user = db.query(User).one()
    user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    response = app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_a_successful_login_resets_the_counter(app_client, account, db):
    for _ in range(3):
        _fail_once(app_client)
    app_client.post(
        "/login", data={"email": "anne@agency.com", "password": PASSWORD},
    )
    db.expire_all()
    assert db.query(User).one().failed_count == 0
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_auth.py -v -k "lock or counter or failures"`
Expected: FAIL — `locked_until` stays `None`, and a correct password succeeds
after ten failures.

- [x] **Step 3: Implement the lockout**

In `renewal/web/auth.py`, add below `WRONG`:

```python
# Ten is far above any plausible typo streak and far below what a password
# guesser needs. The lock is on the account, not the address: it is checked
# only after a row is found, so it never confirms that an address exists.
MAX_FAILURES = 10
LOCKOUT_MINUTES = 15
```

and add this function above `register`:

```python
def _record_failure(session, user: User, now: datetime) -> None:
    user.failed_count += 1
    if user.failed_count >= MAX_FAILURES:
        user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
        user.failed_count = 0
        logger.warning("account locked user_id=%s", user.id)
```

In the `login` handler, Task 4 left this:

```python
            ok = verify_password(
                password, user.password_hash if user else DUMMY_HASH
            )
            if user is None or not user.is_active or not ok:
                session.commit()
                logger.info("login rejected email=%s", email.strip().lower())
                return _page(request, error=WRONG, status=200)
```

Replace it with the version that reads and writes the lock:

```python
            now = datetime.now(timezone.utc)
            locked = (
                user is not None
                and user.locked_until is not None
                and user.locked_until > now
            )
            ok = verify_password(
                password, user.password_hash if user else DUMMY_HASH
            )
            if user is None or not user.is_active or locked or not ok:
                # A locked account records no further failures: otherwise a
                # guesser holds the lock open by continuing to guess.
                if user is not None and not locked:
                    _record_failure(session, user, now)
                session.commit()
                logger.info("login rejected email=%s", email.strip().lower())
                return _page(request, error=WRONG, status=200)
```

The password is still verified while locked, and the result still discarded.
Skipping the hash there would make a locked account answer measurably faster
than an unlocked one, which is the timing signal the dummy hash exists to
suppress.

`failed_count` resets to zero when the lock is set, so the counter starts
clean when the lock expires rather than locking again on the next single typo.

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_auth.py -v`
Expected: PASS. This file now does around thirty scrypt derivations at ~150 ms
each, so expect it to take a few seconds.

- [x] **Step 5: Commit**

```bash
git add renewal/web/auth.py tests/test_web_auth.py
git commit -m "feat(auth): lock an account after ten failed attempts"
```

---

### Task 6: The account-creation script

**Files:**
- Create: `scripts/add_user.py`
- Test: `tests/test_add_user.py`

**Interfaces:**
- Consumes: `hash_password` (Task 1), `renewal.models.User` (Task 2).
- Produces: `scripts.add_user.upsert_user(session, email, password, display_name) -> tuple[User, bool]` — the bool is True when the account was created, False when an existing password was reset.

The `main()` entry point handles the TTY; `upsert_user` holds the logic and is
what the tests drive. The password is never accepted as an argument: argv
lands in shell history and in the process table.

- [x] **Step 1: Write the failing test**

Create `tests/test_add_user.py`:

```python
"""Account creation from the command line.

There is no signup route. For an agency of a few people, a script that the
person with shell access runs is the whole provisioning story.
"""

import pytest

from renewal.auth.passwords import verify_password
from renewal.models import User
from scripts.add_user import MIN_LENGTH, upsert_user


def test_a_new_address_creates_an_account(session):
    user, created = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    assert created
    assert verify_password("a-long-enough-password", user.password_hash)


def test_an_existing_address_resets_the_password(session):
    upsert_user(session, "anne@agency.com", "a-long-enough-password", "Anne")
    user, created = upsert_user(
        session, "anne@agency.com", "a-different-password", "Anne"
    )
    assert not created
    assert session.query(User).count() == 1
    assert verify_password("a-different-password", user.password_hash)


def test_a_reset_clears_a_lockout(session):
    """Resetting the password is also how a locked-out person gets back in."""
    user, _ = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    user.failed_count = 7
    user.locked_until = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    session.flush()
    user, _ = upsert_user(
        session, "anne@agency.com", "a-different-password", "Anne"
    )
    assert user.failed_count == 0
    assert user.locked_until is None


def test_a_reset_reactivates_a_deactivated_account(session):
    user, _ = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    user.is_active = False
    session.flush()
    user, _ = upsert_user(
        session, "anne@agency.com", "a-long-enough-password", "Anne"
    )
    assert user.is_active


def test_the_address_is_matched_case_insensitively(session):
    upsert_user(session, "anne@agency.com", "a-long-enough-password", "Anne")
    _, created = upsert_user(
        session, "Anne@Agency.com", "a-long-enough-password", "Anne"
    )
    assert not created
    assert session.query(User).count() == 1


def test_a_short_password_is_refused(session):
    with pytest.raises(ValueError, match=str(MIN_LENGTH)):
        upsert_user(session, "anne@agency.com", "short", "Anne")


def test_a_password_of_only_spaces_is_refused(session):
    with pytest.raises(ValueError):
        upsert_user(session, "anne@agency.com", " " * 20, "Anne")
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_add_user.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.add_user'`

- [x] **Step 3: Write the script**

Create `scripts/add_user.py`:

```python
"""Create an account, or reset the password on one that exists.

    python -m scripts.add_user anne@agency.com

The password is read from the terminal, never from an argument: argv lands in
shell history and is visible in the process table to every other user on the
machine.

There is no signup route and no reset email. For an agency of a few people,
the person with shell access is the provisioning system, and this is the whole
of it.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from renewal.auth.passwords import hash_password
from renewal.db import get_engine
from renewal.models import User

MIN_LENGTH = 12


def upsert_user(
    session: Session, email: str, password: str, display_name: str
) -> tuple[User, bool]:
    """Returns the account and whether it was created. Resetting also clears a
    lockout and reactivates a deactivated account — a reset is how somebody
    locked out gets back in."""
    if len(password.strip()) < MIN_LENGTH:
        raise ValueError(f"password must be at least {MIN_LENGTH} characters")
    address = email.strip()
    user = session.scalar(
        select(User).where(func.lower(User.email) == address.lower())
    )
    created = user is None
    if user is None:
        user = User(email=address, password_hash="", display_name=display_name)
        session.add(user)
    user.password_hash = hash_password(password)
    user.failed_count = 0
    user.locked_until = None
    user.is_active = True
    session.flush()
    return user, created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument(
        "--name", default=None, help="display name; defaults to the address"
    )
    args = parser.parse_args(argv)

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1

    session = sessionmaker(bind=get_engine())()
    try:
        user, created = upsert_user(
            session, args.email, password, args.name or args.email.strip()
        )
        session.commit()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        session.close()

    print(f"{'Created' if created else 'Password reset for'} {user.email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_add_user.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add scripts/add_user.py tests/test_add_user.py
git commit -m "feat(auth): create and reset accounts from the command line"
```

---

### Task 7: Close the walls — default-deny middleware

This is the task that actually secures the application. It is also the one
that breaks every existing web test, because those tests build an app and call
it with no cookie. The fixture updates are part of this task, not a follow-up:
the suite must be green at the commit.

**Files:**
- Create: `renewal/web/security.py`
- Modify: `renewal/web/__init__.py` — install the middleware in `create_app`
- Modify: `renewal/templates/base.html` — signed-in address and a logout button
- Create: `tests/authhelp.py` — the shared fixture helper
- Modify: the app fixture in `tests/test_web_attention.py`, `tests/test_web_calendar.py`, `tests/test_web_clients.py`, `tests/test_web_comparison.py`, `tests/test_web_review.py`, `tests/test_web_search.py`, `tests/test_web_settings.py`, `tests/test_web_unmatched.py`
- Modify: `README.md` and `.env.example` — the sharpened webhook warning
- Test: `tests/test_web_gate.py`

**Interfaces:**
- Consumes: `lookup_session(session, token, *, ttl_hours)`, `COOKIE_NAME` (Task 3); `safe_next` (Task 4). NOTE: `lookup_session` takes a required keyword-only `ttl_hours` — the plan's Task 3 text originally omitted it and was corrected during execution.
- Produces: `renewal.web.security.install(app, deps) -> None`; `is_public(path) -> bool`; the module constants `PUBLIC_EXACT`, `PUBLIC_PREFIXES`, `PUBLIC_PATTERNS`, `ORIGIN_EXEMPT`, `SAFE_METHODS`; and two request attributes set on every authenticated request, `request.state.user_email` and `request.state.user_display_name`. Plain strings, not a `User` — the ORM object belongs to a session that has already closed by the time a template renders.

`tests/test_web_mail.py` is deliberately **not** in the modify list. Its
fixtures must keep working untouched — that is the test that `/inbound/mail`
stayed public. `tests/test_web_auth.py` is not in it either: those tests sign
themselves in.

- [x] **Step 1: Write the failing test**

Create `tests/test_web_gate.py`:

```python
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
    """Not a 403 from the gate. Whatever the route itself answers is fine."""
    response = signed_in.post(
        "/attention/999999/done",
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code != 403


def test_a_post_with_no_origin_is_allowed(signed_in):
    """curl and the app's own fetch() calls send none. SameSite=Lax is what
    stops a cross-site form post; the Origin check is the second lock."""
    response = signed_in.post(
        "/attention/999999/done", follow_redirects=False
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
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_web_gate.py -v`
Expected: FAIL — protected pages return 200 instead of 303, because nothing
enforces anything yet.

- [x] **Step 3: Write the middleware**

Create `renewal/web/security.py`:

```python
"""The gate.

Default deny. Every request needs a session except the five paths listed
below, and a route added next month is protected the day it is written.

The alternative — a dependency on each router — was rejected because there are
eleven register() sites plus the index route defined inline in this package,
and an omission at any one of them is a silent hole that no test catches. The
failure mode here is a locked door rather than an open one.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from starlette.responses import PlainTextResponse, RedirectResponse

from renewal.auth.sessions import COOKIE_NAME, lookup_session
from renewal.web.deps import Deps

PUBLIC_EXACT = frozenset({"/login", "/logout"})
PUBLIC_PREFIXES = ("/static/",)

# Both of these carry their own credential and are called by something that
# cannot present a cookie: a phone's calendar client, and a mail provider.
PUBLIC_PATTERNS = (
    re.compile(r"^/calendar/[^/]+\.ics$"),
    re.compile(r"^/inbound/mail$"),
)

# The mail provider sends no Origin and authenticates by signature. Every
# other state-changing route is same-origin or nothing.
ORIGIN_EXEMPT = frozenset({"/inbound/mail"})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    if path.startswith(PUBLIC_PREFIXES):
        return True
    return any(pattern.match(path) for pattern in PUBLIC_PATTERNS)


def _origin_ok(request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        # curl, the app's own fetch() calls, and the mail provider send none.
        # SameSite=Lax is what stops a cross-site form post; this is the
        # second lock, not the first.
        return True
    parsed = urlsplit(origin)
    return (parsed.hostname, parsed.port) == (
        request.url.hostname, request.url.port
    )


def install(app, deps: Deps) -> None:
    session_factory = deps.session_factory

    @app.middleware("http")
    async def gate(request, call_next):
        path = request.url.path

        if request.method not in SAFE_METHODS and path not in ORIGIN_EXEMPT:
            if not _origin_ok(request):
                return PlainTextResponse("cross-site request", status_code=403)

        if is_public(path):
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        # Read out as plain strings rather than kept as an ORM object: the
        # session closes here, and a template rendering a detached instance
        # later would raise. The topbar needs an address, nothing more.
        identity: tuple[str, str] | None = None
        if token:
            with session_factory() as session:
                user = lookup_session(
                    session, token,
                    ttl_hours=deps.settings.session_ttl_hours,
                )
                if user is not None:
                    identity = (user.email, user.display_name)
                # Committed either way: lookup_session slides the expiry on a
                # hit and sweeps nothing on a miss.
                session.commit()

        if identity is None:
            if request.method in SAFE_METHODS:
                target = path
                if request.url.query:
                    target = f"{path}?{request.url.query}"
                return RedirectResponse(
                    f"/login?next={quote(target, safe='/')}", status_code=303
                )
            return PlainTextResponse("sign in first", status_code=403)

        request.state.user_email, request.state.user_display_name = identity
        return await call_next(request)
```

`/logout` is in `PUBLIC_EXACT` so that clicking it with an already-dead
session clears the cookie and lands on the login page, rather than being
refused by the gate it is trying to leave.

- [x] **Step 4: Install it and expose the user to templates**

In `renewal/web/__init__.py`, import and call it. It must be installed
**after** the routers are registered — Starlette applies `@app.middleware`
around the whole application, so order of registration relative to routes does
not matter for dispatch, but keeping the call last makes the reading order
match the request order. Add at the end of `create_app`, just before
`return app`:

```python
    security.install(app, deps)
    return app
```

with `from renewal.web import security` added to the imports.

Templates need the address. `Jinja2Templates` passes `request` into every
context already, so `base.html` reads it off `request.state`. In
`renewal/templates/base.html`, replace the `.themepick` block's opening — put
this immediately before `<div class="themepick"`:

```html
    {% if request.state.user_email is defined %}
    <form class="whoami" method="post" action="/logout">
      <span>{{ request.state.user_email }}</span>
      <button type="submit">Sign out</button>
    </form>
    {% endif %}
```

Append to `renewal/static/app.css`:

```css
/* Who is signed in, and the way out. Quiet: it is not the job. */
.whoami {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.85rem;
  color: var(--muted);
}
```

If `--muted` is not the existing token name for de-emphasised text, use
whichever token `.muted` uses in `app.css`.

- [ ] **Step 5: Run the gate tests**

Run: `.venv/bin/pytest tests/test_web_gate.py -v`
Expected: PASS.

- [ ] **Step 6: Confirm the rest of the suite now fails, and see how**

Run: `.venv/bin/pytest`
Expected: FAIL — a large number of failures across `tests/test_web_*.py`,
all of them a 303 where a 200 was expected. `tests/test_web_mail.py` and
`tests/test_web_auth.py` must **not** be among them. If a mail test fails, the
allowlist is wrong; fix that before going on.

- [x] **Step 7: Write the shared test helper**

Create `tests/authhelp.py`:

```python
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
```

- [x] **Step 8: Update the eight app fixtures**

Each of these files builds an app and yields a `TestClient`. Two shapes exist.

**Shape A — the fixture yields inside a `with TestClient(app)` block**
(`test_web_attention.py`, `test_web_calendar.py`, `test_web_clients.py`,
`test_web_search.py`, `test_web_settings.py`, `test_web_unmatched.py`).
Add the import at the top of the file:

```python
from tests.authhelp import sign_in
```

and add one line inside the `with` block, before the `yield`:

```python
    with TestClient(app) as test_client:
        sign_in(test_client, engine)
        yield test_client
```

**Shape B — the fixture returns the app, and each test wraps it**
(`test_web_comparison.py`, `test_web_review.py`). These return
`create_app(...)` directly. Find where each test builds its `TestClient` and
add `sign_in(test_client, engine)` immediately after. If a file wraps in more
than two places, add a small local fixture instead:

```python
@pytest.fixture
def signed_client(app, engine):
    with TestClient(app) as test_client:
        sign_in(test_client, engine)
        yield test_client
```

and switch that file's tests to take `signed_client`.

The `sign_in` account is created with `session.commit()`, so it survives into
the request the app makes on its own connection. It is removed by the
`clean_db` fixture's `TRUNCATE` between tests, which is why `sign_in` checks
for the row before inserting rather than assuming.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/pytest`
Expected: PASS — every test.

If a settings test fails on the ics feed URL, check that
`test_web_settings.py` reads the token from the page rather than from a
hard-coded path; the feed itself is public and unchanged.

- [x] **Step 10: Sharpen the webhook warning**

Auth does not close the webhook. Make that harder to miss.

In `.env.example`, extend the existing `INBOUND_PROVIDER` comment with a final
line:

```
# Adding authentication to the application does NOT close this: /inbound/mail
# is deliberately outside the login, because the caller is a mail provider
# that cannot sign in. Its only protection is the provider's own signature
# check, and filedrop performs none.
```

Add to `README.md`, after the "Blob encryption" section:

```markdown
## Accounts

Every page requires a login. Accounts are created from the command line:

```bash
.venv/bin/python -m scripts.add_user anne@agency.com
```

It prompts for the password twice and never takes it as an argument. Running
it again for an address that already exists resets that password, clears any
lockout, and reactivates a deactivated account — that is also how somebody
locked out gets back in. Ten failed attempts lock an account for fifteen
minutes.

Sessions live in the database, so signing out revokes the session rather than
just dropping the cookie. `SESSION_TTL_HOURS` (default 12) sets the length,
and an active session slides rather than expiring mid-task.

Two routes are outside the login, because neither caller can sign in:

- `/calendar/{token}.ics` — the feed carries its own revocable token. Anyone
  holding that URL reads every client name and deadline without logging in.
  Regenerate it from the settings page.
- `/inbound/mail` — the webhook is verified by the inbound provider's
  signature. With `INBOUND_PROVIDER=filedrop` nothing is verified at all, so
  that provider must never be configured on an install reachable from the
  network.

There are no roles: every account can do everything. Nothing yet records
*which* account made a correction or confirmed a date.
```

- [ ] **Step 11: Run the full suite one more time**

Run: `.venv/bin/pytest`
Expected: PASS.

- [ ] **Step 12: Verify by hand that the application actually starts**

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.add_user you@example.com
SESSION_COOKIE_SECURE=false .venv/bin/uvicorn renewal.app:app --port 8000
```

Check in a browser: `/` redirects to `/login`; a wrong password says
`Email or password is wrong.`; the right one lands on the runs page with the
address in the topbar; Sign out returns to `/login` and `/` redirects again.

- [ ] **Step 13: Commit**

```bash
git add renewal/web/security.py renewal/web/__init__.py \
        renewal/templates/base.html renewal/static/app.css \
        tests/authhelp.py tests/test_web_gate.py tests/test_web_*.py \
        README.md .env.example
git commit -m "feat(auth): close every route that is not deliberately public"
```

---

## Verification

After Task 7, all of this must hold:

- `.venv/bin/pytest` is green.
- `git grep -n "is_public\|PUBLIC_" renewal/` shows the allowlist in exactly
  one module.
- Starting the app and requesting `/` with no cookie gives a 303 to `/login`.
- `curl -i localhost:8000/calendar/<token>.ics` returns a calendar with no
  cookie.
- `curl -i -X POST localhost:8000/inbound/mail` returns the route's own 403
  (`not verified`), not the gate's.

## What this deliberately does not do

Recorded so a reviewer does not read these as omissions:

- No route records *which* user acted. `Correction`, `DateEvent`,
  `AttentionEvent`, `Reclassification`, `ManualDateEvent` and the promotion
  path are untouched, by decision.
- No roles or permissions. Every account can do everything.
- No password reset by email, no signup, no in-app user administration.
- The ics token still grants full read of client names and deadlines to anyone
  holding the URL. That was already true and is stated on the settings page;
  adding a login may make it *feel* addressed when it is not.
- Nothing is scoped per user. Every account sees the whole book, which is
  correct for one agency and wrong the moment there are two.
