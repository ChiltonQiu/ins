"""Turning a received email into rows.

Routing is by recipient. Each agency has a unique intake address and mail is
matched against it — never against the sender, which is forgeable and which
forwarded mail gets wrong anyway. Mail that matches nothing is stored with
status 'quarantined' and produces no documents: dropping it silently would
make a misconfigured forwarding rule invisible.

The body becomes a document alongside the attachments. The carrier's
explanation is frequently in the body while the attachment is a bare form, and
deadlines are very often stated in prose. Its blob is the raw MIME it shares
with the message row, and its page-1 text is the extracted body, so it flows
through search and date extraction exactly like a PDF does.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.mail.provider import InboundEmail
from renewal.models import Agency, Document, DocumentText, InboundMessage
from renewal.pipeline import (
    ingest_document, run_classify_stage, run_dates_stage, run_resolve_stage,
)
from renewal.providers import ModelClient
from renewal.text.store import TEXT_VERSION

logger = logging.getLogger(__name__)

_ADDRESS = re.compile(r"[\w.+-]+@[\w.-]+")
PDF_TYPES = ("application/pdf", "application/x-pdf")


def agency_for(session: Session, to_address: str) -> Agency | None:
    found = _ADDRESS.search(to_address or "")
    if not found:
        return None
    return session.scalar(
        select(Agency).where(
            func.lower(Agency.intake_address) == found.group(0).casefold()
        )
    )


def receive(
    session: Session,
    store: BlobStore,
    email: InboundEmail,
    *,
    client: ModelClient,
    settings: Settings,
) -> InboundMessage:
    raw_digest = store.put(email.raw_mime, ext="eml")
    agency = agency_for(session, email.to_address)

    if agency is None:
        message = InboundMessage(
            agency_id=None, message_id=email.message_id,
            from_address=email.from_address, to_address=email.to_address,
            subject=email.subject, received_at=email.received_at,
            raw_mime_blob_sha256=raw_digest, body_text="",
            processing_status="quarantined",
        )
        session.add(message)
        session.flush()
        logger.warning(
            "mail quarantined message_row_id=%s reason=unknown_intake_address",
            message.id,
        )
        return message

    existing = session.scalar(
        select(InboundMessage)
        .where(InboundMessage.agency_id == agency.id)
        .where(InboundMessage.message_id == email.message_id)
    )
    if existing is not None:
        # Return the row we already have. Writing a second row would mean
        # forging a message_id to get past the unique constraint, corrupting
        # the exact key dedupe depends on. The blob is already stored and
        # deduplicated by content, so a re-delivery costs nothing.
        logger.info(
            "mail duplicate message_row_id=%s agency_id=%s", existing.id, agency.id
        )
        return existing

    message = InboundMessage(
        agency_id=agency.id, message_id=email.message_id,
        from_address=email.from_address, to_address=email.to_address,
        subject=email.subject, received_at=email.received_at,
        raw_mime_blob_sha256=raw_digest, body_text=email.body_text,
        # 'received' exists so a crash mid-processing leaves evidence of what
        # was in flight rather than a row claiming success.
        processing_status="received",
    )
    session.add(message)
    session.flush()

    body_document = Document(
        blob_sha256=raw_digest, original_filename=f"{email.subject or 'message'}.eml",
        page_count=1, has_text_layer=True,
        # Distinct from the dec_page marker the structured extractor looks for,
        # so an email body can never be picked up by that path.
        doc_type="email_body",
        source="email_body", agency_id=agency.id, inbound_message_id=message.id,
    )
    session.add(body_document)
    session.flush()
    session.add(
        DocumentText(
            document_id=body_document.id, page_number=1, text=email.body_text,
            extraction_method="email_body", extractor_version=TEXT_VERSION,
        )
    )
    session.flush()
    run_resolve_stage(session, body_document)
    run_dates_stage(session, body_document, client=client, settings=settings)
    run_classify_stage(session, body_document, client=client, settings=settings)

    for attachment in email.attachments:
        if attachment.content_type not in PDF_TYPES:
            logger.info(
                "attachment skipped message_row_id=%s content_type=%s",
                message.id, attachment.content_type,
            )
            continue
        ingest_document(
            session, store, data=attachment.data,
            original_filename=attachment.filename, source="email_attachment",
            agency_id=agency.id, inbound_message_id=message.id,
            model_client=client, settings=settings,
        )

    # The one place a row written here is updated, inside the same transaction
    # that created it — the row is never observed in its intermediate state.
    message.processing_status = "processed"
    session.flush()
    return message
