"""Optical character recognition, locally.

Tesseract runs on this machine. No scanned page is transmitted to a model
provider for text extraction, which keeps the always-on ingest path free of
third-party exposure however PROVIDER is set.

Tesseract is weaker than a vision model on faxed and skewed pages. That is
accepted: this path feeds search recall and date-shaped strings, not structured
field accuracy.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


class TesseractUnavailable(RuntimeError):
    """The system binary is missing. Raised rather than silently returning an
    empty string, which would record a scanned page as legitimately blank."""


def ocr_page(png: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment problem
        raise TesseractUnavailable(str(exc)) from exc

    try:
        with Image.open(io.BytesIO(png)) as image:
            return pytesseract.image_to_string(image)
    except pytesseract.TesseractNotFoundError as exc:
        raise TesseractUnavailable("tesseract binary not on PATH") from exc
