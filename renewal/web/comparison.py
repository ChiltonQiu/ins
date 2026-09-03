"""The comparison screen: what changed, what it means, and the draft that
explains it.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case

from renewal.comparison import breakdown_for, reclassify
from renewal.draft import latest_draft, save_edit
from renewal.models import (
    Client,
    Comparison,
    Difference,
    Policy,
    PolicyTerm,
    Reclassification,
)
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

# Material differences are the conversation with the client; noise is only
# there to be audited.
_MATERIALITY_ORDER = case(
    (Difference.materiality == "material", 0),
    (Difference.materiality == "informational", 1),
    else_=2,
)


def register(app, deps: Deps) -> None:
    settings = deps.settings
    store = deps.store
    model_client = deps.model_client
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
    def show_comparison(request: Request, comparison_id: int, show_noise: int = 0):
        with session_factory() as session:
            comparison = session.get(Comparison, comparison_id)
            if comparison is None:
                raise HTTPException(status_code=404, detail="no such comparison")
            term = session.get(PolicyTerm, comparison.prior_term_id)
            policy = session.get(Policy, term.policy_id)
            client = session.get(Client, policy.client_id)

            query = session.query(Difference).filter_by(comparison_id=comparison_id)
            if not show_noise:
                query = query.filter(Difference.materiality != "noise")
            differences = query.order_by(_MATERIALITY_ORDER, Difference.field_path).all()

            # A reclassification is recorded, not applied: the rule still says
            # what the row is. The screen shows both so the disagreement is
            # visible instead of looking like a control that did nothing.
            disagreements = {
                row.difference_id: row
                for row in session.query(Reclassification)
                .filter(
                    Reclassification.difference_id.in_(
                        [difference.id for difference in differences]
                    )
                )
                .order_by(Reclassification.id)
            }

            return TEMPLATES.TemplateResponse(
                request,
                "comparison.html",
                {
                    "comparison": comparison,
                    "policy": policy,
                    "client": client,
                    "differences": differences,
                    "disagreements": disagreements,
                    "breakdown": breakdown_for(session, comparison),
                    "draft": latest_draft(session, comparison_id),
                    "show_noise": bool(show_noise),
                },
            )

    @router.post("/comparisons/{comparison_id}/draft")
    def edit_draft(comparison_id: int, final_text: str = Form(...)):
        with session_factory() as session:
            draft = latest_draft(session, comparison_id)
            if draft is None:
                raise HTTPException(status_code=404, detail="no draft yet")
            save_edit(session, draft, final_text)
            session.commit()
        return RedirectResponse(f"/comparisons/{comparison_id}", status_code=303)

    @router.post("/differences/{difference_id}/reclassify", status_code=204)
    def reclassify_difference(
        difference_id: int,
        to_materiality: str = Form(...),
        note: str | None = Form(None),
    ):
        with session_factory() as session:
            difference = session.get(Difference, difference_id)
            if difference is None:
                raise HTTPException(status_code=404, detail="no such difference")
            reclassify(session, difference, to_materiality, note=note)
            session.commit()
        return Response(status_code=204)

    app.include_router(router)
