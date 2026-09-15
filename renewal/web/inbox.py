"""The front door.

Replaces the run list. Every document that has arrived, newest first, grouped
by whether anything is left to do about it. The grouping is computed in
renewal/inbox.py; this module parses the request and renders.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from renewal.inbox import anything_in_flight, bucketed, inbox_rows
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def inbox(request: Request):
        with session_factory() as session:
            groups = bucketed(inbox_rows(session))
            return TEMPLATES.TemplateResponse(
                request,
                "inbox.html",
                {"groups": groups, "in_flight": anything_in_flight(session)},
            )

    app.include_router(router)
