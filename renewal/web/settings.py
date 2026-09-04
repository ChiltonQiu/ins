"""The feed and the one credential it hangs on.

The .ics URL carries a bearer token. It ends up in her phone's calendar
configuration, so anyone holding the link can read every client name and every
deadline in the book until the token is regenerated. The settings page says
that in words rather than assuming she will infer it.
"""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from renewal.calendarview.agenda import DEFAULT_STATUSES, agenda
from renewal.calendarview.ics import render_ics
from renewal.carriers import (
    add_alias, admitted_status, normalize_name, resolve_carrier, set_admitted,
    unresolved_carrier_names,
)
from renewal.models import (
    Agency, Carrier, CarrierAdmittedStatus, CarrierAlias, Policy,
    PolicyBillingType,
)
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

AGENCY_ID = 1

# Every value below is a human decision recorded as a row. None of it is ever
# inferred from a document, so an invalid one is a bug in the form, not input
# to be coerced into something plausible.
ADMITTED_STATUSES = ("admitted", "non_admitted", "unknown")
BILLING_TYPES = ("direct_bill", "agency_bill", "unknown")
_STATE = re.compile(r"[A-Za-z]{2}")


def _one_of(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be one of {', '.join(allowed)}",
        )
    return value


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
            carriers = []
            for carrier in session.scalars(
                select(Carrier).order_by(Carrier.display_name)
            ):
                carriers.append({
                    "carrier": carrier,
                    "aliases": list(session.scalars(
                        select(CarrierAlias.alias)
                        .where(CarrierAlias.carrier_id == carrier.id)
                        .order_by(CarrierAlias.alias)
                    )),
                    "states": list(session.execute(
                        select(CarrierAdmittedStatus.state,
                               CarrierAdmittedStatus.status)
                        .where(CarrierAdmittedStatus.carrier_id == carrier.id)
                        .order_by(CarrierAdmittedStatus.state,
                                  CarrierAdmittedStatus.id.desc())
                    ).all()),
                })
            policies = list(session.execute(
                select(Policy.id, Policy.carrier_name, Policy.policy_number)
                .order_by(Policy.id)
            ).all())
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                {
                    "agency": agency,
                    "feed_url": str(feed_url),
                    "carriers": carriers,
                    "unresolved": unresolved_carrier_names(session),
                    "policies": policies,
                    "admitted_statuses": ADMITTED_STATUSES,
                    "billing_types": BILLING_TYPES,
                },
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

    @router.post("/settings/carriers")
    def create_carrier(display_name: str = Form(...)):
        with session_factory() as session:
            if resolve_carrier(session, display_name) is not None:
                # Refused rather than merged: a second carrier row for the same
                # company splits its admitted statuses in two, and nobody
                # notices until one of them is read.
                raise HTTPException(
                    status_code=409, detail="that carrier already exists"
                )
            session.add(Carrier(display_name=" ".join(display_name.split())))
            session.commit()
        return RedirectResponse("/settings", status_code=303)

    def _add_alias(carrier_id: int, alias: str):
        with session_factory() as session:
            if session.get(Carrier, carrier_id) is None:
                raise HTTPException(status_code=404, detail="no such carrier")
            existing = session.scalar(
                select(CarrierAlias).where(
                    CarrierAlias.alias == normalize_name(alias)
                )
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409, detail="that alias is already taken"
                )
            add_alias(session, carrier_id, alias)
            session.commit()
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/carriers/{carrier_id}/alias")
    def create_alias(carrier_id: int, alias: str = Form(...)):
        return _add_alias(carrier_id, alias)

    @router.post("/settings/aliases")
    def create_alias_for_chosen_carrier(
        carrier_id: int = Form(...), alias: str = Form(...)
    ):
        """Same operation, carrier chosen from a select rather than the path.

        A <select> cannot change a form's action without scripting, and the
        unresolved-names list is exactly where picking the carrier from a list
        is the whole interaction."""
        return _add_alias(carrier_id, alias)

    @router.post("/settings/carriers/{carrier_id}/admitted")
    def set_admitted_status(
        carrier_id: int, state: str = Form(...), status: str = Form(...)
    ):
        _one_of(status, ADMITTED_STATUSES, "status")
        if not _STATE.fullmatch(state.strip()):
            raise HTTPException(
                status_code=422, detail="state must be a two-letter code"
            )
        with session_factory() as session:
            if session.get(Carrier, carrier_id) is None:
                raise HTTPException(status_code=404, detail="no such carrier")
            set_admitted(session, carrier_id, state.strip(), status)
            session.commit()
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/policies/{policy_id}/billing-type")
    def set_billing_type(policy_id: int, billing_type: str = Form(...)):
        _one_of(billing_type, BILLING_TYPES, "billing_type")
        with session_factory() as session:
            if session.get(Policy, policy_id) is None:
                raise HTTPException(status_code=404, detail="no such policy")
            # Appended, never updated: what she believed last month is part of
            # the record, and a payment_due date is scored against whichever
            # answer was current when it was extracted.
            session.add(PolicyBillingType(
                policy_id=policy_id, billing_type=billing_type, set_by="human"
            ))
            session.commit()
        return RedirectResponse("/settings", status_code=303)

    app.include_router(router)
