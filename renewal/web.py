"""FastAPI application. Routes parse the request and call the library; no
pipeline logic lives here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.comparison import breakdown_for, build_comparison, reclassify
from renewal.config import Settings
from renewal.corrections import effective_values, record_correction
from renewal.draft import generate_draft, latest_draft, save_edit
from renewal.extract.runner import extract
from renewal.extract.validate import verification_rate
from renewal.ingest import ingest_pdf
from renewal.materiality import load_rules
from renewal.models import (
    Client,
    Comparison,
    Difference,
    Document,
    ExtractedField,
    Extraction,
    Policy,
    PolicyTerm,
    RenewalRun,
)
from renewal.promote import PromotionBlocked, promote, unresolved_field_paths

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(*, settings: Settings, store: BlobStore, model_client, session_factory):
    app = FastAPI()
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with session_factory() as session:
            runs = (
                session.query(RenewalRun, Client, Policy)
                .join(Policy, RenewalRun.policy_id == Policy.id)
                .join(Client, Policy.client_id == Client.id)
                .order_by(RenewalRun.id.desc())
                .all()
            )
            return TEMPLATES.TemplateResponse(
                request, "index.html", {"runs": runs}
            )

    @app.get("/runs/new", response_class=HTMLResponse)
    def new_run(request: Request, error: str | None = None):
        with session_factory() as session:
            policies = (
                session.query(Client, Policy)
                .join(Policy, Policy.client_id == Client.id)
                .order_by(Client.display_name)
                .all()
            )
            clients = session.query(Client).order_by(Client.display_name).all()
            return TEMPLATES.TemplateResponse(
                request,
                "run_new.html",
                {"policies": policies, "clients": clients, "error": error},
            )

    @app.post("/clients")
    def add_client(display_name: str = Form(...)):
        with session_factory() as session:
            session.add(Client(display_name=display_name))
            session.commit()
        return RedirectResponse("/runs/new", status_code=303)

    @app.post("/policies")
    def add_policy(
        client_id: int = Form(...),
        carrier_name: str = Form(...),
        policy_number: str = Form(...),
        line_of_business: str = Form(...),
    ):
        with session_factory() as session:
            session.add(
                Policy(
                    client_id=client_id,
                    carrier_name=carrier_name,
                    policy_number=policy_number,
                    line_of_business=line_of_business,
                )
            )
            session.commit()
        return RedirectResponse("/runs/new", status_code=303)

    @app.post("/runs")
    async def create_run(
        policy_id: int = Form(...),
        prior: UploadFile = ...,
        renewal: UploadFile = ...,
        confirm_same: str | None = Form(None),
    ):
        prior_bytes = await prior.read()
        renewal_bytes = await renewal.read()
        same = hashlib.sha256(prior_bytes).digest() == hashlib.sha256(
            renewal_bytes
        ).digest()
        if same and not confirm_same:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Both slots hold the same document. Tick the confirmation box "
                    "if that is deliberate."
                ),
            )

        with session_factory() as session:
            prior_doc = ingest_pdf(
                session, store, data=prior_bytes, original_filename=prior.filename
            )
            renewal_doc = ingest_pdf(
                session, store, data=renewal_bytes, original_filename=renewal.filename
            )
            run = RenewalRun(
                policy_id=policy_id,
                prior_document_id=prior_doc.id,
                renewal_document_id=renewal_doc.id,
            )
            session.add(run)
            session.flush()
            for document in (prior_doc, renewal_doc):
                extract(
                    session,
                    store,
                    document,
                    "v1",
                    client=model_client,
                    settings=settings,
                )
            session.commit()
            run_id = run.id
        return RedirectResponse(f"/runs/{run_id}/review", status_code=303)

    def _latest_extraction(session: Session, document_id: int) -> Extraction:
        return (
            session.query(Extraction)
            .filter_by(document_id=document_id)
            .order_by(Extraction.id.desc())
            .first()
        )

    @app.get("/runs/{run_id}/review", response_class=HTMLResponse)
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
                    .order_by(ExtractedField.field_path)
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

    @app.post("/fields/{field_id}/correct", status_code=204)
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

    @app.post("/fields/{field_id}/reject", status_code=204)
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

    @app.post("/extractions/{extraction_id}/fields", status_code=204)
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

    @app.post("/runs/{run_id}/promote")
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

    @app.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
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
            differences = query.order_by(Difference.field_path).all()

            return TEMPLATES.TemplateResponse(
                request,
                "comparison.html",
                {
                    "comparison": comparison,
                    "policy": policy,
                    "client": client,
                    "differences": differences,
                    "breakdown": breakdown_for(session, comparison),
                    "draft": latest_draft(session, comparison_id),
                    "show_noise": bool(show_noise),
                },
            )

    @app.post("/comparisons/{comparison_id}/draft")
    def edit_draft(comparison_id: int, final_text: str = Form(...)):
        with session_factory() as session:
            draft = latest_draft(session, comparison_id)
            if draft is None:
                raise HTTPException(status_code=404, detail="no draft yet")
            save_edit(session, draft, final_text)
            session.commit()
        return RedirectResponse(f"/comparisons/{comparison_id}", status_code=303)

    @app.post("/differences/{difference_id}/reclassify", status_code=204)
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

    return app
