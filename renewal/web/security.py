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
