"""The inbound-mail webhook.

Two rules govern the status codes here, and they pull in opposite directions.

A hosted provider retries any non-200 indefinitely, so every outcome that is
merely *unwanted* — an unknown recipient, a duplicate delivery, MIME we cannot
parse — returns 200 and records what happened in processing_status. Retrying
those forever would bury the real signal and never succeed.

A failed verification is different. An unverified caller is not evidence about
anything, so it gets 403 and nothing is written at all: accepting it would let
anyone who can reach this route post mail into the record as any carrier.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from renewal.mail.intake import receive
from renewal.mail.provider import InboundProvider
from renewal.models import InboundMessage
from renewal.web.deps import Deps

logger = logging.getLogger(__name__)


def register(app, deps: Deps, provider: InboundProvider) -> None:
    session_factory = deps.session_factory
    store = deps.store
    settings = deps.settings
    model_client = deps.model_client

    router = APIRouter()

    @router.post("/inbound/mail")
    async def inbound_mail(request: Request):
        body = await request.body()
        headers = dict(request.headers)

        if not provider.verify(headers, body):
            # Nothing is written. An unverified caller is not evidence.
            logger.warning("inbound mail rejected reason=verification_failed")
            raise HTTPException(status_code=403, detail="not verified")

        try:
            email = provider.parse(headers, body)
        except Exception:  # noqa: BLE001
            # The raw bytes are kept so the failure can be diagnosed and the
            # message re-processed after a parser fix.
            logger.exception("inbound mail parse failed")
            with session_factory() as session:
                digest = store.put(body, ext="eml")
                message = InboundMessage(
                    agency_id=None, message_id=f"sha256:{digest}",
                    from_address="", to_address="", subject=None,
                    received_at=datetime.now(timezone.utc),
                    raw_mime_blob_sha256=digest, body_text="",
                    processing_status="failed",
                )
                session.add(message)
                session.commit()
                row_id = message.id
            logger.info("inbound mail recorded message_row_id=%s status=failed", row_id)
            return JSONResponse({"status": "failed"})

        with session_factory() as session:
            message = receive(session, store, email,
                              client=model_client, settings=settings)
            session.commit()
            row_id, status = message.id, message.processing_status
        # Ids and statuses only. Never the body, never the subject.
        logger.info("inbound mail recorded message_row_id=%s status=%s",
                    row_id, status)
        return JSONResponse({"status": status})

    app.include_router(router)
