"""The count beside the inbox link.

A middleware rather than a per-route context entry: the nav is on every page,
and thirteen routes each remembering to pass the same number is twelve chances
to forget.
"""

from __future__ import annotations

import logging
import re

from renewal.inbox import needs_you_count
from renewal.web.deps import Deps

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD"})

# The PDF route, and only it. /documents/{id}/review is an HTML page and needs
# the badge like every other page; matching the prefix would silently strip it
# from the one screen she reaches most often from a needs-you row.
_PDF_ROUTE = re.compile(r"^/documents/\d+$")


def _renders_nav(request) -> bool:
    path = request.url.path
    if request.method not in SAFE_METHODS:
        return False
    # A static file, a PDF download and a 204 from a correction endpoint all
    # render no nav, so the query would be waste.
    return not path.startswith("/static/") and not _PDF_ROUTE.match(path)


def install(app, deps: Deps) -> None:
    session_factory = deps.session_factory

    @app.middleware("http")
    async def badge(request, call_next):
        if _renders_nav(request):
            try:
                with session_factory() as session:
                    request.state.needs_you_count = needs_you_count(session)
            except Exception:  # noqa: BLE001 - a badge must never 500 a page
                logger.exception("badge count failed")
        return await call_next(request)
