from __future__ import annotations

from dataclasses import dataclass

import fitz

# A dec page that really has a text layer carries far more than this. Scanned
# pages sometimes carry a stray watermark string, which this threshold ignores.
MIN_CHARS_FOR_TEXT_LAYER = 100


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based, matching what the model is asked to cite
    text: str


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    has_text_layer: bool
    pages: list[PageText]


def read_pdf(data: bytes) -> PdfInfo:
    with fitz.open(stream=data, filetype="pdf") as doc:
        pages = [PageText(i + 1, page.get_text("text")) for i, page in enumerate(doc)]
    has_text_layer = any(
        len("".join(page.text.split())) >= MIN_CHARS_FOR_TEXT_LAYER for page in pages
    )
    return PdfInfo(page_count=len(pages), has_text_layer=has_text_layer, pages=pages)


def layout_text(info: PdfInfo) -> str:
    """Page-delimited text for the prompt, so the model can cite page numbers."""
    return "\n".join(
        f"=== PAGE {page.page_number} ===\n{page.text}" for page in info.pages
    )


def rasterize(data: bytes, dpi: int = 200) -> list[bytes]:
    pngs: list[bytes] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pngs.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    return pngs
