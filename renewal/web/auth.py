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

# Ten is far above any plausible typo streak and far below what a password
# guesser needs. The lock is on the account, not the address: it is checked
# only after a row is found, so it never confirms that an address exists.
MAX_FAILURES = 10
LOCKOUT_MINUTES = 15


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


def _record_failure(session, user: User, now: datetime) -> None:
    user.failed_count += 1
    if user.failed_count >= MAX_FAILURES:
        user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
        user.failed_count = 0
        logger.warning("account locked user_id=%s", user.id)


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
