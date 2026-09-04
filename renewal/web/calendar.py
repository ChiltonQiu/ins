"""The calendar. Agenda is the default, because a list on paper is what this
replaces.

Confirmation is one click and dismissal is one keystroke, because the value of
over-extraction depends entirely on clearing a wrong date being cheaper than
missing a right one. An unconfirmed date never renders in the same style as a
confirmed one: the system is not allowed to show a guess as a fact.
"""

from __future__ import annotations

import calendar as stdcalendar
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from renewal.calendarview.agenda import ALL_STATUSES, agenda
from renewal.dates.service import confirm, dismiss
from renewal.models import Client, ManualDate, ManualDateEvent
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

# There is no auth and no tenancy yet; every screen is this one agency's.
AGENCY_ID = 1


def _back_to(request: Request) -> str:
    """Return to the calendar she was looking at, filters and all. A confirm
    that dropped her filters would cost more than it saved.

    Only the path and query of the referrer are ever used, and only when the
    referrer is one of our own calendar URLs. Redirecting to a whole referrer
    URL would let any page that links here choose where this app sends her
    next, and honouring a foreign origin's query string would let it choose
    what she sees when she lands.
    """
    parts = urlsplit(request.headers.get("referer", ""))
    if parts.netloc and parts.netloc != request.url.netloc:
        return "/calendar"
    if parts.path == "/calendar" or parts.path.startswith("/calendar/"):
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/calendar"



def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    store = deps.store

    router = APIRouter()

    def _entries(session, client_id, date_type, status):
        # No status filter means the agenda's own default, which hides
        # dismissed dates. Asking for one narrows to exactly that status.
        extra = {"statuses": (status,)} if status else {}
        return agenda(
            session,
            agency_id=AGENCY_ID,
            client_id=client_id,
            date_types=(date_type,) if date_type else None,
            **extra,
        )

    def _context(session, request, view, client_id, date_type, status):
        entries = _entries(session, client_id, date_type, status)
        clients = session.query(Client).order_by(Client.display_name).all()
        return {
            "entries": entries,
            "clients": clients,
            "view": view,
            "selected_client_id": client_id,
            "selected_date_type": date_type,
            "selected_status": status,
            "statuses": ALL_STATUSES,
        }

    @router.get("/calendar", response_class=HTMLResponse)
    def show_agenda(
        request: Request,
        client_id: int | None = None,
        date_type: str | None = None,
        status: str | None = None,
    ):
        with session_factory() as session:
            context = _context(
                session, request, "agenda", client_id, date_type, status
            )
            return TEMPLATES.TemplateResponse(request, "calendar.html", context)

    @router.get("/calendar/month", response_class=HTMLResponse)
    def show_month(
        request: Request,
        year: int | None = None,
        month: int | None = None,
        client_id: int | None = None,
        date_type: str | None = None,
        status: str | None = None,
    ):
        today = date.today()
        year = year or today.year
        month = month or today.month
        with session_factory() as session:
            context = _context(
                session, request, "month", client_id, date_type, status
            )
            # Grouped by day so the template renders a grid rather than
            # re-scanning the whole list for every cell.
            by_day: dict[date, list] = {}
            for entry in context["entries"]:
                by_day.setdefault(entry.date_value, []).append(entry)
            context.update(
                year=year,
                month=month,
                month_name=stdcalendar.month_name[month],
                weeks=stdcalendar.Calendar(firstweekday=6).monthdatescalendar(
                    year, month
                ),
                by_day=by_day,
                today=today,
            )
            return TEMPLATES.TemplateResponse(request, "calendar.html", context)

    @router.post("/dates/{document_date_id}/confirm")
    def confirm_date(request: Request, document_date_id: int):
        with session_factory() as session:
            confirm(session, document_date_id)
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    @router.post("/dates/{document_date_id}/dismiss")
    def dismiss_date(request: Request, document_date_id: int):
        with session_factory() as session:
            dismiss(session, document_date_id)
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    @router.post("/manual-dates")
    def add_manual_date(
        request: Request,
        title: str = Form(...),
        date_value: date = Form(...),
        date_type: str = Form(...),
        client_id: int | None = Form(None),
        notes: str | None = Form(None),
    ):
        with session_factory() as session:
            session.add(
                ManualDate(
                    agency_id=AGENCY_ID, client_id=client_id, title=title,
                    date_value=date_value, date_type=date_type, notes=notes,
                    created_by="human",
                )
            )
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    @router.post("/manual-dates/{manual_date_id}/dismiss")
    def dismiss_manual_date(request: Request, manual_date_id: int):
        with session_factory() as session:
            session.add(
                ManualDateEvent(manual_date_id=manual_date_id,
                                action="dismissed", actor="human")
            )
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    app.include_router(router)
