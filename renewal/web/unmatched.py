"""Filing a document against a client.

The queue this module used to render is now a bucket of the inbox, but the two
fixes stayed here: every assignment here is a human decision that gets recorded
with the alternatives it was chosen over. That record is the training data for
making matching better, and it is the reason assignment writes a row rather
than setting a field.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import RedirectResponse

from renewal.models import Client, Document
from renewal.resolve.service import assign, candidates_for
from renewal.web.deps import Deps


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    router = APIRouter()

    @router.post("/unmatched/{document_id}/assign")
    def assign_document(
        document_id: int,
        client_id: int = Form(...),
        policy_id: int | None = Form(None),
    ):
        with session_factory() as session:
            if session.get(Client, client_id) is None:
                raise HTTPException(status_code=404, detail="no such client")
            document = _document_or_404(session, document_id)
            # Recomputed rather than taken from the form: what she chose over
            # has to be what the system actually offered, not what a stale page
            # or a hand-built request claims it offered.
            matches = candidates_for(session, document)
            assign(
                session, document_id, client_id=client_id, policy_id=policy_id,
                candidates=[asdict(m) for m in matches],
            )
            session.commit()
        return RedirectResponse("/", status_code=303)

    @router.post("/unmatched/{document_id}/new-client")
    def new_client_for_document(
        document_id: int, display_name: str = Form(...)
    ):
        with session_factory() as session:
            _document_or_404(session, document_id)
            client = Client(display_name=display_name)
            session.add(client)
            session.flush()
            # No candidates: nothing was offered, which is why she is typing a
            # name. An empty list records that honestly.
            assign(session, document_id, client_id=client.id, policy_id=None,
                   candidates=[])
            session.commit()
        return RedirectResponse("/", status_code=303)

    app.include_router(router)


def _document_or_404(session, document_id: int) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="no such document")
    return document
