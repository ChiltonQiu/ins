"""The client overview: everything about one client on one screen.

Also the home of /documents/{id}, which every "one click to the PDF" link in
the app points at. It lives here rather than beside the calendar because it
belongs to the record, not to one view of it.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from renewal.blobstore import BlobNotFound
from renewal.calendarview.agenda import agenda
from renewal.clients.overview import overview
from renewal.models import Client, Document, DocumentLink, Policy
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

AGENCY_ID = 1

# A filename reaches us from whoever uploaded the document. A quote would end
# the quoted string early and a newline would start a header of the attacker's
# choosing, so the header carries only characters that can mean neither.
_UNSAFE_IN_FILENAME = re.compile(r'[^A-Za-z0-9 ._-]')


def content_disposition(filename: str) -> str:
    safe = _UNSAFE_IN_FILENAME.sub("_", filename).strip() or "document.pdf"
    return f'inline; filename="{safe[:120]}"'


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    store = deps.store
    router = APIRouter()

    @router.get("/clients", response_class=HTMLResponse)
    def list_clients(request: Request):
        with session_factory() as session:
            counts = dict(session.execute(
                select(Policy.client_id, func.count(Policy.id))
                .group_by(Policy.client_id)
            ).all())
            entries = agenda(session, agency_id=AGENCY_ID)
            nearest: dict[int, object] = {}
            for entry in sorted(entries, key=lambda e: e.date_value):
                if entry.client_id is not None:
                    nearest.setdefault(entry.client_id, entry)
            rows = [
                {
                    "client": client,
                    "policy_count": counts.get(client.id, 0),
                    "next_date": nearest.get(client.id),
                }
                for client in session.scalars(
                    select(Client).order_by(Client.display_name)
                )
            ]
            return TEMPLATES.TemplateResponse(
                request, "clients.html", {"rows": rows}
            )

    @router.get("/clients/{client_id}", response_class=HTMLResponse)
    def show_client(request: Request, client_id: int):
        with session_factory() as session:
            try:
                got = overview(session, client_id, agency_id=AGENCY_ID)
            except LookupError:
                raise HTTPException(status_code=404, detail="no such client")
            return TEMPLATES.TemplateResponse(
                request, "client.html", {"o": got}
            )

    @router.get("/documents/{document_id}")
    def show_document(document_id: int):
        """The source behind every row that cites one. Every claim in this app
        has to be one click from the page it was read off."""
        with session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise HTTPException(status_code=404, detail="no such document")
            sha = document.blob_sha256
            filename = document.original_filename
        try:
            content = store.get(sha)
        except BlobNotFound:
            # The row survives without its bytes; say so rather than 500.
            raise HTTPException(status_code=404, detail="the file is missing")
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": content_disposition(filename)},
        )

    app.include_router(router)
