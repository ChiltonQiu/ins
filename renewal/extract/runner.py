"""Extraction: a versioned, re-runnable function of a document.

Nothing here mutates an earlier extraction. A retry, a prompt change, or a new
model all produce new extraction rows, so any version can be re-run over the
whole corpus and the results compared.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.extract import prompt_v1
from renewal.extract.schema import parse_payload
from renewal.extract.validate import validate_fields
from renewal.models import Document, ExtractedField, Extraction
from renewal.pdftext import PdfInfo, layout_text, rasterize, read_pdf
from renewal.providers import ModelClient, image_block, text_block

logger = logging.getLogger(__name__)

PROMPTS = {prompt_v1.VERSION: prompt_v1}


def _build_content(prompt, data: bytes, pdf: PdfInfo, has_text_layer: bool) -> list[dict]:
    """Neutral content IR. Wire format belongs to the client, not here."""
    if has_text_layer:
        return [
            text_block(prompt.USER_TEXT_TEMPLATE.format(document_text=layout_text(pdf)))
        ]
    blocks = [text_block(prompt.USER_IMAGE_INSTRUCTION)]
    blocks.extend(image_block(png) for png in rasterize(data))
    return blocks


def extract(
    session: Session,
    store: BlobStore,
    document: Document,
    version: str,
    *,
    client: ModelClient,
    settings: Settings,
) -> Extraction:
    prompt = PROMPTS[version]

    def _record(status: str, raw: dict | None) -> Extraction:
        extraction = Extraction(
            document_id=document.id,
            extractor_version=version,
            model_id=f"{settings.provider}:{settings.extraction_model}",
            raw_response=raw,
            status=status,
        )
        session.add(extraction)
        session.flush()
        # Ids, hashes, counts, status. Never field values or document text.
        logger.info(
            "extraction id=%s document_id=%s sha256=%s version=%s status=%s",
            extraction.id,
            document.id,
            document.blob_sha256,
            version,
            status,
        )
        return extraction

    try:
        data = store.get(document.blob_sha256)
        pdf = read_pdf(data)
        content = _build_content(prompt, data, pdf, document.has_text_layer)
        raw_text = client.complete(
            model=settings.extraction_model, system=prompt.SYSTEM, content=content
        )
    except Exception as exc:  # noqa: BLE001 - the failure itself is the record
        return _record("failed", {"error": str(exc)})

    try:
        payload = parse_payload(raw_text)
    except Exception:  # noqa: BLE001 - unparseable text is kept verbatim
        return _record("invalid_response", {"text": raw_text})

    validated = validate_fields(payload, pdf, settings.confidence_threshold)
    status = "partial" if any(v.validation_error for v in validated) else "ok"
    extraction = _record(status, {"text": raw_text})
    for item in validated:
        session.add(
            ExtractedField(
                extraction_id=extraction.id,
                field_path=item.payload.field_path,
                value=item.payload.value,
                confidence=item.confidence,
                source_page=item.payload.source_page,
                source_text_span=item.payload.source_text,
                validation_error=item.validation_error,
                needs_review=item.needs_review,
            )
        )
    session.flush()
    session.refresh(extraction)
    return extraction
