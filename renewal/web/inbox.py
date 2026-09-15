"""The front door.

Replaces the run list. Every document that has arrived, newest first, grouped
by whether anything is left to do about it. The grouping is computed in
renewal/inbox.py; this module parses the request and renders.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case

from renewal.background import process_document
from renewal.corrections import effective_values
from renewal.extract.validate import verification_rate
from renewal.inbox import anything_in_flight, bucketed, inbox_rows, state_of
from renewal.ingest import ingest_pdf
from renewal.models import Document, ExtractedField, Extraction
from renewal.promote import unresolved_field_paths
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

AGENCY_ID = 1

# Checked by content rather than by the filename or the browser's guess at the
# content type, both of which are supplied by whoever is uploading.
PDF_MAGIC = b"%PDF-"

# Reading order. Alphabetical puts policy.total_premium below every coverage
# and form line, and the premium is the first thing anyone checks. Within a
# group the order stays alphabetical.
_FIELD_GROUP = case(
    (ExtractedField.field_path.like("policy.%"), 0),
    (ExtractedField.field_path.like("coverage.%"), 1),
    (ExtractedField.field_path.like("item.%"), 2),
    (ExtractedField.field_path.like("forms.%"), 3),
    else_=4,
)


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

    @router.post("/documents/{document_id}/retry")
    def retry_document(
        document_id: int, acknowledged: list[str] = Form(default=[])
    ):
        """Re-run the stages for one document.

        Safe on anything: every stage checks its own work before doing it, and
        promotion is idempotent per extraction and per blob. A retry of a
        document that already finished writes nothing.
        """
        with session_factory() as session:
            row = session.get(Document, document_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such document")
            row.status = "processing"
            # Reset the clock, or a retried document is stalled the instant it
            # is retried.
            row.status_changed_at = datetime.now(timezone.utc)
            session.commit()

        deps.runner.submit(
            process_document,
            session_factory,
            store,
            document_id,
            model_client=model_client,
            settings=settings,
            acknowledged=frozenset(acknowledged),
        )
        return RedirectResponse("/", status_code=303)

    @router.get("/documents/{document_id}/review", response_class=HTMLResponse)
    def review_document(request: Request, document_id: int):
        """One document, its fields, and its fix.

        The stuck-documents spec's single-document screen. It serves both the
        needs_review and the nothing_extracted rows, because the fix for both
        is the same table: correct what is wrong, add what is missing, then
        file it.
        """
        with session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise HTTPException(status_code=404, detail="no such document")
            state = state_of(session, document)

            extraction = (
                session.query(Extraction)
                .filter_by(document_id=document_id)
                .order_by(Extraction.id.desc())
                .first()
            )
            fields, extra, blocked, rate = [], [], [], None
            if extraction is not None:
                values = effective_values(session, extraction.id)
                fields = (
                    session.query(ExtractedField)
                    .filter_by(extraction_id=extraction.id)
                    .order_by(_FIELD_GROUP, ExtractedField.field_path)
                    .all()
                )
                for field in fields:
                    field.effective_value = values.get(
                        field.field_path, field.value
                    )
                emitted = {field.field_path for field in fields}
                extra = [
                    (path, value) for path, value in values.items()
                    if path not in emitted
                ]
                blocked = unresolved_field_paths(session, extraction.id)
                rate = verification_rate(fields)

            return TEMPLATES.TemplateResponse(
                request,
                "inbox_detail.html",
                {
                    "document": document,
                    "state": state,
                    "extraction": extraction,
                    "fields": fields,
                    "extra_fields": extra,
                    "blocked": blocked,
                    "rate": rate,
                },
            )

    app.include_router(router)
