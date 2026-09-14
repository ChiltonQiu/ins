"""Choosing what a comparison holds, and the screen that reads it back.

The picker writes columns; the comparison screen renders them as a matrix of
one baseline and up to four comparands.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from renewal.comparison import (
    MAX_COLUMNS,
    ColumnSpec,
    ColumnsRejected,
    build_matrix,
    matrix_for,
    reclassify,
)
from renewal.draft import latest_draft, save_edit
from renewal.materiality import load_rules
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


def register(app, deps: Deps) -> None:
    settings = deps.settings
    store = deps.store
    model_client = deps.model_client
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/policies/{policy_id}/compare", response_class=HTMLResponse)
    def new_comparison(
        request: Request,
        policy_id: int,
        baseline: int | None = None,
        comparand: list[int] = Query(default=[]),
        error: str | None = None,
    ):
        """Terms of one policy, bound and quoted listed apart.

        baseline and comparand preselect, which is how the renewal_received
        attention item arrives here with both terms already ticked.
        """
        with session_factory() as session:
            policy = session.get(Policy, policy_id)
            if policy is None:
                raise HTTPException(status_code=404, detail="no such policy")
            terms = (
                session.query(PolicyTerm)
                .filter_by(policy_id=policy_id)
                .order_by(PolicyTerm.id)
                .all()
            )
            return TEMPLATES.TemplateResponse(
                request,
                "compare_new.html",
                {
                    "policy": policy,
                    "client": session.get(Client, policy.client_id),
                    # Listed in the order they were written. Sorting by
                    # premium would be a ranking, and nothing here ranks.
                    "bound": [term for term in terms if term.kind == "bound"],
                    "quoted": [term for term in terms if term.kind == "quoted"],
                    "baseline": baseline,
                    "chosen": set(comparand),
                    "max_comparands": MAX_COLUMNS - 1,
                    "error": error,
                },
            )

    @router.post("/comparisons")
    def create_comparison(
        policy_id: int = Form(...),
        baseline: int = Form(...),
        comparand: list[int] = Form(default=[]),
    ):
        """No draft is written here.

        This can produce a matrix with a quoted column, and a note that sets
        carriers side by side is a recommendation however it is worded. The
        renewal path in review.py is the one that drafts.
        """
        specs = [ColumnSpec(baseline, "baseline")] + [
            ColumnSpec(term_id, "comparand") for term_id in comparand
        ]
        with session_factory() as session:
            try:
                comparison = build_matrix(
                    session,
                    columns=specs,
                    rules=load_rules(settings.materiality_config),
                    settings=settings,
                )
            except ColumnsRejected as rejected:
                raise HTTPException(status_code=400, detail=str(rejected))
            session.commit()
            comparison_id = comparison.id
        return RedirectResponse(f"/comparisons/{comparison_id}", status_code=303)

    @router.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
    def show_comparison(request: Request, comparison_id: int, show_noise: int = 0):
        with session_factory() as session:
            comparison = session.get(Comparison, comparison_id)
            if comparison is None:
                raise HTTPException(status_code=404, detail="no such comparison")
            matrix = matrix_for(session, comparison, include_noise=bool(show_noise))

            # A reclassification is recorded, not applied: the rule still says
            # what the row is. The screen shows both so the disagreement is
            # visible instead of looking like a control that did nothing.
            disagreements = {
                row.difference_id: row
                for row in session.query(Reclassification)
                .filter(
                    Reclassification.difference_id.in_(
                        [row.difference.id for row in matrix.rows]
                    )
                )
                .order_by(Reclassification.id)
            }

            return TEMPLATES.TemplateResponse(
                request,
                "comparison.html",
                {
                    "matrix": matrix,
                    "comparison": comparison,
                    "policy": matrix.policy,
                    "client": matrix.client,
                    "disagreements": disagreements,
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
