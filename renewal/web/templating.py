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


TEMPLATES.env.filters["wbr"] = _wbr
