"""The front door.

Replaces the run list. Every document that has arrived, newest first, grouped
by whether anything is left to do about it. The grouping is computed in
renewal/inbox.py; this module parses the request and renders.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from renewal.background import process_document
from renewal.inbox import anything_in_flight, bucketed, inbox_rows
from renewal.ingest import ingest_pdf
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

AGENCY_ID = 1

# Checked by content rather than by the filename or the browser's guess at the
# content type, both of which are supplied by whoever is uploading.
PDF_MAGIC = b"%PDF-"


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    store = deps.store
    settings = deps.settings
    model_client = deps.model_client
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def inbox(request: Request):
        with session_factory() as session:
            groups = bucketed(inbox_rows(session))
            return TEMPLATES.TemplateResponse(
                request,
                "inbox.html",
                {"groups": groups, "in_flight": anything_in_flight(session)},
            )

    @router.post("/documents")
    async def drop_document(document: UploadFile):
        """The manual backup path. One file, no questions.

        Everything the old two-upload form asked for — which policy, which
        slot, is this really the same file — is either answered by the pipeline
        or asked later on the row that needs it.
        """
        data = await document.read()
        if not data.startswith(PDF_MAGIC):
            raise HTTPException(
                status_code=400,
                detail="That file is not a PDF. Drop the PDF the carrier sent.",
            )

        with session_factory() as session:
            row = ingest_pdf(
                session,
                store,
                data=data,
                original_filename=document.filename or "dropped.pdf",
                source="manual_upload",
                agency_id=AGENCY_ID,
            )
            # Written before the stages run, so the row — the receipt that the
            # file arrived — is on the page the redirect lands on.
            row.status = "processing"
            session.commit()
            document_id = row.id

        deps.runner.submit(
            process_document,
            session_factory,
            store,
            document_id,
            model_client=model_client,
            settings=settings,
        )
        return RedirectResponse("/", status_code=303)

    app.include_router(router)
