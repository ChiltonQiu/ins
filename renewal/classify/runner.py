"""The coarse label, and the routing decision it feeds."""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.classify import prompt_v1
from renewal.config import Settings
from renewal.models import Document, DocumentClassification
from renewal.providers import ModelClient, text_block
from renewal.text.store import page_text

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = prompt_v1.VERSION

DOC_CLASSES = (
    "declarations", "endorsement", "cancellation_notice", "non_renewal_notice",
    "invoice", "id_card", "loss_run", "inspection_report", "quote",
    "correspondence", "unknown",
)

# Only these route on to structured field extraction. quote is classified and
# stored; nothing consumes it in this phase.
FIELD_EXTRACTION_CLASSES = ("declarations", "endorsement")


def parse_classification(raw: str) -> tuple[str, float]:
    """Anything unrecognised collapses to unknown at zero confidence. This
    function never raises: classification gates nothing, so a bad response must
    degrade rather than fail."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return ("unknown", 0.0)
    try:
        payload = json.loads(match.group(0))
        label = payload.get("doc_class")
        confidence = float(payload.get("confidence", 0.0))
    except (ValueError, TypeError):
        return ("unknown", 0.0)
    if label not in DOC_CLASSES:
        return ("unknown", 0.0)
    return (label, confidence)


def latest_class(session: Session, document_id: int) -> str | None:
    return session.scalar(
        select(DocumentClassification.doc_class)
        .where(DocumentClassification.document_id == document_id)
        .order_by(DocumentClassification.id.desc())
        .limit(1)
    )


def classify(
    session: Session, document: Document, *, client: ModelClient, settings: Settings
) -> DocumentClassification:
    text = page_text(session, document.id, 1).strip()
    model_id = f"{settings.provider}:{settings.classification_model}"

    if not text:
        # No page to read. Asking the model to label nothing would spend money
        # to produce a guess about an empty string.
        label, confidence = "unknown", 0.0
    else:
        try:
            raw = client.complete(
                model=settings.classification_model,
                system=prompt_v1.SYSTEM,
                content=[text_block(prompt_v1.USER_TEMPLATE.format(page_text=text))],
            )
            label, confidence = parse_classification(raw)
        except Exception:  # noqa: BLE001 - a declined answer is still an answer
            logger.exception("classification failed document_id=%s", document.id)
            label, confidence = "unknown", 0.0

    # A row is written either way: an absent row and a declined answer are
    # different facts, and only one of them means "not yet attempted".
    row = DocumentClassification(
        document_id=document.id, doc_class=label, confidence=confidence,
        classifier_version=CLASSIFIER_VERSION, model_id=model_id,
    )
    session.add(row)
    session.flush()
    logger.info(
        "classified document_id=%s class=%s confidence=%.2f",
        document.id, label, confidence,
    )
    return row
