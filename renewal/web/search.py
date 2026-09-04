"""The search screen.

One box. An empty query renders the form and nothing else — listing every
document would be a slower, less useful version of the calendar.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from renewal.models import Client
from renewal.search.query import search
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/search", response_class=HTMLResponse)
    def show_search(
        request: Request,
        q: str = "",
        client_id: int | None = None,
        doc_class: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ):
        with session_factory() as session:
            results = (
                search(session, q, client_id=client_id, doc_class=doc_class,
                       start=start, end=end)
                if q.strip() else []
            )
            clients = session.query(Client).order_by(Client.display_name).all()
            return TEMPLATES.TemplateResponse(
                request,
                "search.html",
                {
                    "q": q,
                    "results": results,
                    "searched": bool(q.strip()),
                    "clients": clients,
                    "selected_client_id": client_id,
                    "selected_doc_class": doc_class,
                    "start": start,
                    "end": end,
                },
            )

    app.include_router(router)
