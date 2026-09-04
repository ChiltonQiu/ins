"""The Jinja environment, shared by every router module.

It lives in its own module rather than in the package __init__ so a router can
import it without importing the package that imports the router.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _wbr(path: str) -> Markup:
    """Let a long field path wrap at its dots. Breaking inside a segment
    ("deductib / le_value") costs a beat every time you scan the column."""
    return Markup(escape(path).replace(".", Markup(".<wbr>")))


def _headline(snippet: str) -> Markup:
    """Render a ts_headline result.

    The snippet is document text with <mark> tags inserted by Postgres. The
    text between those tags came off a page someone else wrote, so the whole
    string is escaped first and only the highlight tags are put back. Marking
    the snippet safe would hand every document in the archive an HTML
    injection into this page.
    """
    escaped = escape(snippet)
    return Markup(
        str(escaped).replace("&lt;mark&gt;", "<mark>").replace(
            "&lt;/mark&gt;", "</mark>"
        )
    )


TEMPLATES.env.filters["wbr"] = _wbr
TEMPLATES.env.filters["headline"] = _headline
