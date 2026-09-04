"""Build eval fixtures from real documents.

Reads a real PDF's text, substitutes identifying values, and regenerates a
synthetic PDF from the result. Redacting a PDF in place is unreliable —
covered text stays in the content stream — so this regenerates instead. The
output is not a faithful copy of the original's layout, and it is not meant to
be: it is a fixture.

Named substitutions are explicit. Pattern-based ones catch what a human would
forget: VINs, tax-id-shaped numbers, and long digit runs.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import fitz

from renewal.pdftext import read_pdf

_PATTERNS = (
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "1XXXXXXXXXXXXXXXX"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "000-00-0000"),
    (re.compile(r"\b\d{9,}\b"), "000000000"),
)


def redact_text(text: str, *, substitutions: dict[str, str]) -> str:
    for old, new in substitutions.items():
        text = text.replace(old, new)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_pdf(data: bytes, *, substitutions: dict[str, str]) -> bytes:
    out = fitz.open()
    for page in read_pdf(data).pages:
        new = out.new_page()
        y = 72
        for line in redact_text(page.text, substitutions=substitutions).splitlines():
            new.insert_text((72, y), line, fontsize=11)
            y += 16
    return out.tobytes()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--substitutions", type=Path,
        help="JSON object mapping real values to replacements",
    )
    args = parser.parse_args()
    subs = json.loads(args.substitutions.read_text()) if args.substitutions else {}
    args.destination.write_bytes(
        redact_pdf(args.source.read_bytes(), substitutions=subs)
    )
    print(f"wrote {args.destination}")


if __name__ == "__main__":
    main()
