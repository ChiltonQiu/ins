"""The comparison screen: what changed, what it means, and the draft that
explains it.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from renewal.comparison import matrix_for, reclassify
from renewal.draft import latest_draft, save_edit
from renewal.models import (
    Comparison,
    Difference,
    Reclassification,
)
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES


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
            # Transitional: the screen is still two fixed columns and the
            # template still says Prior and Renewal. What moved is where the
            # values come from — difference.prior_value and renewal_value are
            # not written any more, so the cells are read off the matrix and
            # handed over beside the rows. The next task takes the template to
            # N columns and this mapping goes with it.
            matrix = matrix_for(session, comparison, include_noise=bool(show_noise))
            policy, client = matrix.policy, matrix.client
            differences = [row.difference for row in matrix.rows]
            cells = {
                row.difference.id: (row.baseline.value, row.comparands[0].value)
                for row in matrix.rows
            }

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
                    "cells": cells,
                    "disagreements": disagreements,
                    "breakdown": matrix.columns[1].breakdown,
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
