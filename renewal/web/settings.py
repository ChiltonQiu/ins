"""The feed and the one credential it hangs on.

The .ics URL carries a bearer token. It ends up in her phone's calendar
configuration, so anyone holding the link can read every client name and every
deadline in the book until the token is regenerated. The settings page says
that in words rather than assuming she will infer it.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from renewal.calendarview.agenda import DEFAULT_STATUSES, agenda
from renewal.calendarview.ics import render_ics
from renewal.models import Agency
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

AGENCY_ID = 1


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/calendar/{token}.ics")
    def ics_feed(token: str):
        with session_factory() as session:
            agency = session.scalar(select(Agency).where(Agency.ics_token == token))
            if agency is None:
                # No hint about whether the token is wrong or merely retired.
                raise HTTPException(status_code=404, detail="no such calendar")
            entries = agenda(
                session, agency_id=agency.id, statuses=DEFAULT_STATUSES
            )
            body = render_ics(
                entries, calendar_name=f"{agency.display_name} deadlines"
            )
        return Response(content=body, media_type="text/calendar; charset=utf-8")

    @router.get("/settings", response_class=HTMLResponse)
    def show_settings(request: Request):
        with session_factory() as session:
            agency = session.get(Agency, AGENCY_ID)
            if agency is None:
                raise HTTPException(status_code=404, detail="no agency")
            feed_url = request.url_for("ics_feed", token=agency.ics_token)
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                {"agency": agency, "feed_url": str(feed_url)},
            )

    @router.post("/settings/regenerate-ics-token")
    def regenerate_ics_token():
        with session_factory() as session:
            agency = session.get(Agency, AGENCY_ID)
            if agency is None:
                raise HTTPException(status_code=404, detail="no agency")
            # The one field on agency updated in place. An append-only row
            # would leave the old token still matching, and the whole point of
            # regenerating is that the leaked link stops working now.
            agency.ics_token = secrets.token_urlsafe(32)
            session.commit()
        return RedirectResponse("/settings", status_code=303)

    app.include_router(router)
