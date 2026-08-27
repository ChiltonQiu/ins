"""Synthetic PDFs for tests. Never use a real client document here."""

from __future__ import annotations

import fitz


def make_text_pdf(pages: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    return doc.tobytes()


def make_scanned_pdf(pages: list[list[str]]) -> bytes:
    """Same content, rendered to images so there is no text layer."""
    source = fitz.open(stream=make_text_pdf(pages), filetype="pdf")
    out = fitz.open()
    for page in source:
        pix = page.get_pixmap(dpi=150)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    return out.tobytes()
