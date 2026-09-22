"""The front page.

One read-only route. Everything it shows is computed in renewal/overview.py,
which is where the reasoning lives; this file only decides what a request
turns into.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from renewal.overview import overview
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    settings = deps.settings

    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with session_factory() as session:
            return TEMPLATES.TemplateResponse(
                request, "overview.html", overview(session, settings=settings)
            )

    app.include_router(router)
