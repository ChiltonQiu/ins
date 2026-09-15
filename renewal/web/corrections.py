"""Correcting one extracted field.

Three endpoints, posted to by app.js from the document detail view. They keep
the URLs they had under the run flow because app.js posts to them by literal
path: moving them would break every correction silently, with a 404 nobody
sees.

Every correction is a row rather than an edit. The corrections table is the
long-term asset — it is the training data for making extraction better — which
is why a value reverted to what the extractor said is still recorded.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import Response

from renewal.corrections import record_correction
from renewal.models import ExtractedField
from renewal.web.deps import Deps


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    router = APIRouter()

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

    app.include_router(router)
