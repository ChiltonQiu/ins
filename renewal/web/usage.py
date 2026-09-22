"""Recording that a page was used.

A middleware, like the nav badge, for the same reason: every route would
otherwise have to remember, and thirteen routes remembering is twelve chances
to forget.

What it stores is the route template, the method, the status, how long the
request took, and which page it came from. What it does not store is the path
with its ids in it, the query string, any form field, any filename and any
client. That line is deliberate and it is the whole design: this log exists to
be exported and read somewhere else, and a usage log that accumulates client
detail is a second copy of the record layer sitting somewhere with none of its
protections.

Recording never fails a request. A page that 500s because the analytics could
not be written would be a worse product than one with no analytics at all.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

from renewal.models import UsageEvent
from renewal.web.deps import Deps

logger = logging.getLogger(__name__)

# Traffic rather than use. The PDF route is a file download, the feed is a
# calendar client polling on its own schedule, and static files are the
# browser doing its job — none of them is somebody deciding to look at
# something.
_IGNORED = re.compile(r"^/static/|^/documents/\d+$|\.ics$|^/inbound/mail$")


def _template(request, fallback: str) -> str:
    """The matched route's path, not the requested one.

    '/documents/{document_id}/review' aggregates; '/documents/41/review' is
    forty rows saying the same thing, one of which names a document.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    # No route matched: a 404. Recording the raw path here would be the one
    # place an id could leak in, so it does not.
    return fallback


def _leaf_routes(app) -> list:
    """Every route that can actually match a path.

    app.routes is not that list: each include_router() call appears as one
    object holding its own routes, so the top level is a handful of containers
    and four docs endpoints. Walking it once at install time is both correct
    and cheaper than doing it per request — the table cannot change after the
    application is built.
    """
    found: list = []

    def walk(routes) -> None:
        for route in routes:
            children = getattr(route, "routes", None)
            if not children:
                # FastAPI wraps each include_router() in an object that keeps
                # its children on .original_router rather than .routes, so a
                # walk that only knows about .routes finds the four docs
                # endpoints and nothing this application actually serves.
                inner = getattr(route, "original_router", None)
                children = getattr(inner, "routes", None) if inner else None
            if children:
                walk(children)
            elif getattr(route, "path_regex", None) is not None:
                found.append(route)

    walk(app.routes)
    return found


def _from_route(request, routes) -> str | None:
    """Where she came from, as a route template.

    Taken from the referer and immediately matched against this application's
    own routes, so a foreign referer or a crafted one can never become a row:
    what gets stored is one of our own path templates or nothing.
    """
    referer = request.headers.get("referer", "")
    if not referer:
        return None
    try:
        parts = urlsplit(referer)
    except ValueError:
        return None
    if parts.netloc and parts.netloc != request.url.netloc:
        return None
    for route in routes:
        if route.path_regex.match(parts.path):
            return getattr(route, "path", None)
    return None


def install(app, deps: Deps) -> None:
    if not deps.settings.usage_tracking:
        return

    session_factory = deps.session_factory
    routes = _leaf_routes(app)

    @app.middleware("http")
    async def record(request, call_next):
        if _IGNORED.search(request.url.path):
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        elapsed = int((time.perf_counter() - started) * 1000)

        try:
            with session_factory() as session:
                session.add(UsageEvent(
                    user_id=getattr(request.state, "user_id", None),
                    route=_template(request, "(unmatched)"),
                    method=request.method,
                    status=response.status_code,
                    duration_ms=elapsed,
                    from_route=_from_route(request, routes),
                ))
                session.commit()
        except Exception:  # noqa: BLE001 - analytics must never 500 a page
            logger.exception("usage event not recorded")
        return response
