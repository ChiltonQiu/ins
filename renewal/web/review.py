"""Reviewing an extraction field by field, and promoting the run once the
fields hold up.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case
from sqlalchemy.orm import Session

from renewal.comparison import breakdown_for, build_comparison
from renewal.corrections import effective_values, record_correction
from renewal.draft import generate_draft
from renewal.extract.validate import verification_rate
from renewal.materiality import load_rules
from renewal.models import (
    Client,
    Difference,
    ExtractedField,
    Extraction,
    Policy,
    RenewalRun,
)
from renewal.promote import PromotionBlocked, promote, unresolved_field_paths
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

# Reading order on the review screen. Alphabetical puts policy.total_premium
# below every coverage and form line, and the premium is the first thing anyone
# checks. Within a group the order stays alphabetical.
_FIELD_GROUP = case(
    (ExtractedField.field_path.like("policy.%"), 0),
    (ExtractedField.field_path.like("coverage.%"), 1),
    (ExtractedField.field_path.like("item.%"), 2),
    (ExtractedField.field_path.like("forms.%"), 3),
    else_=4,
)


def register(app, deps: Deps) -> None:
    settings = deps.settings
    store = deps.store
    model_client = deps.model_client
    session_factory = deps.session_factory
    router = APIRouter()

    def _latest_extraction(session: Session, document_id: int) -> Extraction:
        return (
            session.query(Extraction)
            .filter_by(document_id=document_id)
            .order_by(Extraction.id.desc())
            .first()
        )

    @router.get("/runs/{run_id}/review", response_class=HTMLResponse)
    def review(request: Request, run_id: int):
        with session_factory() as session:
            run = session.get(RenewalRun, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="no such run")
            policy = session.get(Policy, run.policy_id)
            client = session.get(Client, policy.client_id)

            sides, extra_fields, blocked, rates = [], {}, [], {}
            for label, document_id in (
                ("Prior", run.prior_document_id),
                ("Renewal", run.renewal_document_id),
            ):
                extraction = _latest_extraction(session, document_id)
                values = effective_values(session, extraction.id)
                fields = (
                    session.query(ExtractedField)
                    .filter_by(extraction_id=extraction.id)
                    .order_by(_FIELD_GROUP, ExtractedField.field_path)
                    .all()
                )
                for field in fields:
                    field.effective_value = values.get(field.field_path, field.value)
                emitted = {field.field_path for field in fields}
                extra_fields[extraction.id] = [
                    (path, value)
                    for path, value in values.items()
                    if path not in emitted
                ]
                blocked.extend(unresolved_field_paths(session, extraction.id))
                rates[extraction.id] = verification_rate(fields)
                sides.append((label, extraction, fields))

            return TEMPLATES.TemplateResponse(
                request,
                "run_review.html",
                {
                    "run": run,
                    "policy": policy,
                    "client": client,
                    "sides": sides,
                    "extra_fields": extra_fields,
                    "blocked": sorted(set(blocked)),
                    "rates": rates,
                },
            )

    @router.post("/fields/{field_id}/correct", status_code=204)
    def correct_field(field_id: int, corrected_value: str = Form(...)):
        with session_factory() as session:
            field = session.get(ExtractedField, field_id)
            if field is None:
                raise HTTPException(status_code=404, detail="no such field")
            record_correction(
                session,
                extraction_id=field.extraction_id,
                extracted_field_id=field.id,
                field_path=field.field_path,
                kind="wrong_value",
                extracted_value=field.value,
                corrected_value=corrected_value,
            )
            session.commit()
        return Response(status_code=204)

    @router.post("/fields/{field_id}/reject", status_code=204)
    def reject_field(field_id: int):
        with session_factory() as session:
            field = session.get(ExtractedField, field_id)
            if field is None:
                raise HTTPException(status_code=404, detail="no such field")
            record_correction(
                session,
                extraction_id=field.extraction_id,
                extracted_field_id=field.id,
                field_path=field.field_path,
                kind="hallucination",
                extracted_value=field.value,
            )
            session.commit()
        return Response(status_code=204)

    @router.post("/extractions/{extraction_id}/fields", status_code=204)
    def add_missing_field(
        extraction_id: int,
        field_path: str = Form(...),
        corrected_value: str = Form(...),
    ):
        with session_factory() as session:
            record_correction(
                session,
                extraction_id=extraction_id,
                field_path=field_path,
                kind="omission",
                corrected_value=corrected_value,
            )
            session.commit()
        return Response(status_code=204)

    @router.post("/runs/{run_id}/promote")
    def promote_run(run_id: int, acknowledged: list[str] = Form(default=[])):
        with session_factory() as session:
            run = session.get(RenewalRun, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="no such run")
            try:
                terms = [
                    promote(
                        session,
                        _latest_extraction(session, document_id),
                        run.policy_id,
                        acknowledged=frozenset(acknowledged),
                    )
                    for document_id in (
                        run.prior_document_id,
                        run.renewal_document_id,
                    )
                ]
            except PromotionBlocked as blocked:
                session.rollback()
                raise HTTPException(
                    status_code=400,
                    detail=f"still needs review: {', '.join(blocked.paths)}",
                )

            comparison = build_comparison(
                session,
                run_id=run.id,
                prior_term=terms[0],
                renewal_term=terms[1],
                rules=load_rules(settings.materiality_config),
            )
            differences = (
                session.query(Difference).filter_by(comparison_id=comparison.id).all()
            )
            generate_draft(
                session,
                comparison,
                differences,
                breakdown_for(session, comparison),
                client=model_client,
                settings=settings,
            )
            session.commit()
            comparison_id = comparison.id
        return RedirectResponse(f"/comparisons/{comparison_id}", status_code=303)

    app.include_router(router)
