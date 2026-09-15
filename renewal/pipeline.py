"""The one way a document enters the system.

Bulk import, manual upload, and email intake all call ingest_document. Every
document is stored, text-extracted, date-extracted, and made searchable —
those stages have no judgment in them. Only some documents go on to structured
field extraction, and only high-confidence extractions are promoted. Search and
the calendar never depend on field-extraction accuracy.

Every stage after storage is best-effort and separately re-runnable. A stage
that raises is logged and skipped: losing the document because OCR failed would
be worse than a document with no text yet, and the stage can be re-run.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from sqlalchemy import select

from renewal.attention.rules import evaluate as evaluate_attention
from renewal.attention.rules import evaluate_promotion
from renewal.blobstore import BlobStore
from renewal.classify.runner import (
    FIELD_EXTRACTION_CLASSES, classify, latest_class,
)
from renewal.config import Settings
from renewal.corrections import effective_values
from renewal.dates.service import extract_dates
from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from renewal.models import Document, DocumentText, Extraction, PolicyTerm
from renewal.pdftext import PageText
from renewal.promote import PromotionBlocked, promote, unresolved_field_paths
from renewal.providers import ModelClient
from renewal.resolve.service import latest_link, resolve_document
from renewal.text.store import TEXT_VERSION, extract_text, has_text

logger = logging.getLogger(__name__)

SOURCES = ("bulk_import", "manual_upload", "email_attachment", "email_body")


def run_text_stage(session: Session, store: BlobStore, document: Document) -> None:
    if has_text(session, document.id):
        return
    try:
        extract_text(session, store, document)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception(
            "text stage failed document_id=%s sha256=%s",
            document.id,
            document.blob_sha256,
        )


def run_resolve_stage(session: Session, document: Document) -> None:
    try:
        resolve_document(session, document)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("resolve stage failed document_id=%s", document.id)


def stored_pages(session: Session, document_id: int) -> list[PageText]:
    """Read the text back out of DocumentText rather than out of the PDF.

    That is what makes date extraction work identically for an email body,
    which has no PDF behind it at all."""
    rows = session.execute(
        select(DocumentText.page_number, DocumentText.text)
        .where(DocumentText.document_id == document_id)
        .where(DocumentText.extractor_version == TEXT_VERSION)
        .order_by(DocumentText.page_number)
    ).all()
    return [PageText(page_number, text) for page_number, text in rows]


def run_dates_stage(
    session: Session,
    document: Document,
    *,
    client: ModelClient | None = None,
    settings: Settings | None = None,
) -> None:
    try:
        extract_dates(
            session, document, stored_pages(session, document.id),
            client=client, settings=settings,
        )
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("dates stage failed document_id=%s", document.id)


def run_classify_stage(
    session: Session, document: Document, *, client: ModelClient, settings: Settings
) -> None:
    if latest_class(session, document.id) is not None:
        return
    try:
        classify(session, document, client=client, settings=settings)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("classify stage failed document_id=%s", document.id)


def should_extract_fields(session: Session, document_id: int) -> bool:
    """Routing only. A document that is not routed here is still stored,
    searchable, and date-extracted."""
    return latest_class(session, document_id) in FIELD_EXTRACTION_CLASSES


def run_fields_stage(
    session: Session,
    store: BlobStore,
    document: Document,
    *,
    client: ModelClient,
    settings: Settings,
) -> None:
    if not should_extract_fields(session, document.id):
        return
    try:
        extract(session, store, document, "v1", client=client, settings=settings)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("fields stage failed document_id=%s", document.id)


def run_promote_stage(
    session: Session,
    document: Document,
    *,
    settings: Settings,
    acknowledged: frozenset[str] = frozenset(),
) -> PolicyTerm | None:
    """Promote what needs no human, and leave everything else alone.

    Phase 2's pipeline diagram specified this stage — "promotion to PolicyTerm,
    only above confidence threshold" — and it was never built, so nothing that
    arrived through the pipe ever became a term.

    Two conditions, both already-recorded facts:

      - the latest document_link carries a policy_id, which under D8 means an
        exact policy-number match, the only auto-link this system performs; and
      - the extraction has no needs_review fields, so promote() would not raise.

    Nothing new is inferred and no threshold is softened. An extraction failing
    either condition waits for a human exactly as it does today.
    """
    link = latest_link(session, document.id)
    if link is None or link.policy_id is None:
        return None

    extraction = (
        session.query(Extraction)
        .filter_by(document_id=document.id)
        .order_by(Extraction.id.desc())
        .first()
    )
    if extraction is None:
        return None

    # An extraction that produced nothing is not a clean extraction. A failed
    # provider call and an unparseable response both record the attempt with
    # no fields at all, so there is nothing flagged and the needs_review check
    # below sees a clean run — and promoting writes a term whose every value
    # is NULL. _latest_term takes the newest bound term, so that empty term
    # becomes what the client page and the prep sheet show for a policy that
    # has a perfectly good prior one, and evaluate_promotion stays quiet
    # because it has no effective date to compare. Silent, and wrong.
    #
    # effective_values rather than the status, because the question is what
    # the extraction is worth and not how it was produced: a human who
    # supplied the fields by hand against a failed extraction has made it
    # promotable, which is how a stuck document gets unstuck.
    if not effective_values(session, extraction.id):
        return None
    # acknowledged is empty on every automatic run: nothing is ever waved
    # through without a human looking at it. It carries a value only when she
    # has opened the document and ticked the flagged path herself.
    if unresolved_field_paths(session, extraction.id, acknowledged):
        return None
    if (
        session.query(PolicyTerm)
        .filter_by(promoted_from_extraction_id=extraction.id)
        .first()
    ):
        return None  # idempotent, like every other stage

    # Content addressing deduplicates the blob but not the document, so the
    # same dec page delivered twice — email and portal, or a resend — arrives
    # as two documents with two extractions. Promoting both would put two
    # identical terms on the chain and raise the renewal twice. Same bytes,
    # same policy, already promoted: an exact-identity check, no judgment.
    twin = session.scalar(
        select(PolicyTerm.id)
        .join(Document, Document.id == PolicyTerm.source_document_id)
        .where(PolicyTerm.policy_id == link.policy_id)
        .where(Document.blob_sha256 == document.blob_sha256)
        .where(PolicyTerm.source_document_id != document.id)
        .limit(1)
    )
    if twin is not None:
        return None

    kind = "quoted" if latest_class(session, document.id) == "quote" else "bound"
    try:
        return promote(
            session, extraction, link.policy_id, kind=kind,
            acknowledged=acknowledged,
        )
    except PromotionBlocked:
        # Unreachable given the check above, and caught anyway: a malformed
        # date is blocked by promote() for a reason the stage cannot see.
        logger.info("promotion blocked document_id=%s", document.id)
        return None


def run_attention_stage(
    session: Session, document: Document, *, settings: Settings
) -> None:
    try:
        evaluate_attention(session, document, settings=settings)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("attention stage failed document_id=%s", document.id)


def run_stages(
    session: Session,
    store: BlobStore,
    document: Document,
    *,
    extract_fields: bool = True,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
    acknowledged: frozenset[str] = frozenset(),
) -> Document:
    """Everything that happens to a document after it is stored.

    Split out of ingest_document so the background task can run it against a
    row that already exists: the row is written inside the request, so the
    receipt appears the instant the file lands, and these stages run after the
    response has gone out.

    Every stage is idempotent, which is what makes the Retry button safe.
    """
    run_text_stage(session, store, document)
    run_resolve_stage(session, document)
    # Optional rather than required: with no model configured the regex pass
    # still runs, and a caller with no model still gets the recall floor
    # instead of no dates at all.
    run_dates_stage(session, document, client=model_client, settings=settings)
    # After dates on purpose: date extraction must not be able to depend on a
    # label, and running it first makes that impossible rather than merely
    # untrue today.
    if model_client is not None and settings is not None:
        run_classify_stage(
            session, document, client=model_client, settings=settings
        )
    # Costs a model call per routed document, which is why the caller can turn
    # it off. Bulk import does; manual upload and email intake do not.
    if extract_fields and model_client is not None and settings is not None:
        run_fields_stage(
            session, store, document, client=model_client, settings=settings
        )
    # After the fields stage, because it promotes what that stage extracted,
    # and before attention, because Task 9's rules read the term it writes.
    if settings is not None:
        term = run_promote_stage(
            session, document, settings=settings, acknowledged=acknowledged,
        )
        if term is not None:
            evaluate_promotion(session, term)
    # Last: its rules read the label and the link that the stages above wrote.
    if settings is not None:
        run_attention_stage(session, document, settings=settings)
    return document


def ingest_document(
    session: Session,
    store: BlobStore,
    *,
    data: bytes,
    original_filename: str,
    source: str,
    agency_id: int,
    inbound_message_id: int | None = None,
    extract_fields: bool = True,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
) -> Document:
    if source not in SOURCES:
        raise ValueError(f"unknown document source: {source!r}")
    document = ingest_pdf(
        session,
        store,
        data=data,
        original_filename=original_filename,
        source=source,
        agency_id=agency_id,
        inbound_message_id=inbound_message_id,
    )
    return run_stages(
        session,
        store,
        document,
        extract_fields=extract_fields,
        model_client=model_client,
        settings=settings,
    )
