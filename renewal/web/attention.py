"""The attention queue.

One list she clears by hand. Nothing here resolves itself, and nothing here
acts on her behalf — the queue only says what appears to need a response.

Two kinds of row arrive together. A materialised row is an AttentionItem
written at ingest, cleared by appending an event to it. A live row has no item
at all: it is computed from an unconfirmed date coming up soon, so it is
cleared by acting on that date instead, which is the right action anyway.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from renewal.attention.rules import open_items, resolve
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

# The same two reasons the calendar pins, marked with the same class, so the
# two screens cannot disagree about what an emergency looks like.
ESCALATED_REASONS = ("cancellation_notice", "non_renewal_notice")


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/attention", response_class=HTMLResponse)
    def show_attention(request: Request):
        with session_factory() as session:
            rows = open_items(session)
            ordered = sorted(
                rows,
                key=lambda r: (
                    r.reason_code not in ESCALATED_REASONS,
                    r.due_date or date.max,
                ),
            )
            return TEMPLATES.TemplateResponse(
                request,
                "attention.html",
                {"rows": ordered, "escalated": ESCALATED_REASONS},
            )

    @router.post("/attention/{item_id}/done")
    def mark_done(item_id: int):
        with session_factory() as session:
            resolve(session, item_id, action="done")
            session.commit()
        return RedirectResponse("/attention", status_code=303)

    @router.post("/attention/{item_id}/dismiss")
    def mark_dismissed(item_id: int):
        with session_factory() as session:
            resolve(session, item_id, action="dismissed")
            session.commit()
        return RedirectResponse("/attention", status_code=303)

    app.include_router(router)
