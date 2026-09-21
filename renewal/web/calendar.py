"""The calendar, as a month grid and as an agenda.

The month is what the header links to, because "how bad is the week of the
14th" is a question about shape and a list cannot answer it. The agenda is
still here and still the place where a date is actually judged: it has room
for the source, the status and the two buttons, which a grid cell does not.

Confirmation is one click and dismissal is one keystroke, because the value of
over-extraction depends entirely on clearing a wrong date being cheaper than
missing a right one. An unconfirmed date never renders in the same style as a
confirmed one: the system is not allowed to show a guess as a fact.
"""

from __future__ import annotations

import calendar as stdcalendar
from datetime import date
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from renewal.calendarview.agenda import ALL_STATUSES, agenda
from renewal.dates.service import confirm, dismiss
from renewal.models import Client, ManualDate, ManualDateEvent
from renewal.web.deps import Deps, acting_user_id
from renewal.web.templating import TEMPLATES

# There is no tenancy yet; every screen is this one agency's, and every
# signed-in account sees all of it.
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

    def _entries(session, client_id, date_type, status, start=None, end=None):
        # No status filter means the agenda's own default, which hides
        # dismissed dates. Asking for one narrows to exactly that status.
        extra = {"statuses": (status,)} if status else {}
        return agenda(
            session,
            agency_id=AGENCY_ID,
            client_id=client_id,
            date_types=(date_type,) if date_type else None,
            start=start,
            end=end,
            **extra,
        )

    def _context(
        session, request, view, client_id, date_type, status,
        start=None, end=None,
    ):
        entries = _entries(session, client_id, date_type, status, start, end)
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
        if not 1 <= month <= 12:
            raise HTTPException(status_code=404, detail="no such month")
        weeks = stdcalendar.Calendar(firstweekday=6).monthdatescalendar(
            year, month
        )
        with session_factory() as session:
            # Windowed to the weeks actually on screen. Without it the query
            # is the agenda's — every date the agency has, capped at 500 — and
            # the month you navigated to is whichever 500 came back first.
            context = _context(
                session, request, "month", client_id, date_type, status,
                start=weeks[0][0], end=weeks[-1][-1],
            )
            # Grouped by day so the template renders a grid rather than
            # re-scanning the whole list for every cell.
            by_day: dict[date, list] = {}
            for entry in context["entries"]:
                by_day.setdefault(entry.date_value, []).append(entry)

            def month_url(y: int, m: int) -> str:
                # Navigation carries the filters. Landing on an unfiltered
                # December because you pressed an arrow in a filtered November
                # is how a filter silently stops being true.
                query = {"year": y, "month": m}
                if client_id is not None:
                    query["client_id"] = client_id
                if date_type:
                    query["date_type"] = date_type
                if status:
                    query["status"] = status
                return "/calendar/month?" + urlencode(query)

            context.update(
                year=year,
                month=month,
                month_name=stdcalendar.month_name[month],
                day_names=[
                    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday",
                ],
                weeks=weeks,
                by_day=by_day,
                today=today,
                month_url=month_url,
                prev_year=year - 1 if month == 1 else year,
                prev_month=12 if month == 1 else month - 1,
                next_year=year + 1 if month == 12 else year,
                next_month=1 if month == 12 else month + 1,
                # The grid drops to marks on a phone, so the same entries are
                # listed underneath it in date order.
                month_entries=sorted(
                    (e for e in context["entries"] if e.date_value.month == month
                     and e.date_value.year == year),
                    key=lambda e: (e.date_value, e.client_name or ""),
                ),
            )
            return TEMPLATES.TemplateResponse(request, "calendar.html", context)

    @router.post("/dates/{document_date_id}/confirm")
    def confirm_date(request: Request, document_date_id: int):
        with session_factory() as session:
            confirm(session, document_date_id,
                    user_id=acting_user_id(request))
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    @router.post("/dates/{document_date_id}/dismiss")
    def dismiss_date(request: Request, document_date_id: int):
        with session_factory() as session:
            dismiss(session, document_date_id,
                    user_id=acting_user_id(request))
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
                    created_by="human", user_id=acting_user_id(request),
                )
            )
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    @router.post("/manual-dates/{manual_date_id}/dismiss")
    def dismiss_manual_date(request: Request, manual_date_id: int):
        with session_factory() as session:
            session.add(
                ManualDateEvent(manual_date_id=manual_date_id,
                                action="dismissed", actor="human",
                                user_id=acting_user_id(request))
            )
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    app.include_router(router)
