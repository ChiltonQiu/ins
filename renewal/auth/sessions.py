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
        # Set explicitly rather than left to the server default, so the row
        # is fully formed without depending on whether the default has been
        # read back yet.
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
    # The window is flat: each use grants another full TTL from now. The span
    # is never re-derived from expires_at, because this function is what
    # moves expires_at — doing so would compound on every hit and let a live
    # session's absolute expiry grow without bound.
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
