"""Setting up a renewal run: the clients and policies it needs, and the two
uploads it compares.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from renewal.models import Client, Policy, RenewalRun
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES


def register(app, deps: Deps) -> None:
    settings = deps.settings
    store = deps.store
    model_client = deps.model_client
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/runs/new", response_class=HTMLResponse)
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

    @router.post("/clients")
    def add_client(display_name: str = Form(...)):
        with session_factory() as session:
            session.add(Client(display_name=display_name))
            session.commit()
        return RedirectResponse("/runs/new", status_code=303)

    @router.post("/policies")
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

    @router.post("/runs")
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

    app.include_router(router)
