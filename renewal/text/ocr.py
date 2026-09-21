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
import os

logger = logging.getLogger(__name__)


class TesseractUnavailable(RuntimeError):
    """The system binary is missing. Raised rather than silently returning an
    empty string, which would record a scanned page as legitimately blank."""


def _configure(pytesseract) -> None:
    """Point pytesseract at the binary when PATH does not.

    The Windows installer puts tesseract.exe under Program Files and does not
    add it to PATH, so the binary is present and unreachable — which from in
    here is indistinguishable from not having it at all. TESSERACT_CMD names
    it. On a machine where PATH is enough the variable is unset and this
    changes nothing.
    """
    command = os.environ.get("TESSERACT_CMD", "").strip()
    if command:
        pytesseract.pytesseract.tesseract_cmd = command


def ocr_page(png: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment problem
        raise TesseractUnavailable(str(exc)) from exc

    _configure(pytesseract)

    try:
        with Image.open(io.BytesIO(png)) as image:
            return pytesseract.image_to_string(image)
    except pytesseract.TesseractNotFoundError as exc:
        raise TesseractUnavailable(
            "tesseract binary not found; put it on PATH or set TESSERACT_CMD "
            "to its full path"
        ) from exc
