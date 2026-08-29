"""Per-field accuracy diff between two extractor versions over the fixtures.

Usage: python scripts/compare_versions.py v1 v2

Makes real API calls. Prints one line per field path whose accuracy moved, so a
change that helps overall but quietly breaks one carrier is still visible.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from evals.accuracy import load_fixtures, score
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.corrections import effective_values
from renewal.db import get_engine
from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from renewal.providers import build_client

FIXTURE_DIR = Path(__file__).parent.parent / "evals" / "fixtures"
PDF_DIR = Path(__file__).parent.parent / "evals" / "pdfs"


def run_version(version: str) -> dict[str, dict[str, bool]]:
    settings = load_settings()
    client = build_client(settings)
    session = sessionmaker(bind=get_engine())()
    out: dict[str, dict[str, bool]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        store = BlobStore(Path(tmp))
        for fixture in load_fixtures(FIXTURE_DIR):
            pdf = PDF_DIR / fixture.pdf_filename
            if not pdf.exists():
                continue
            document = ingest_pdf(
                session, store, data=pdf.read_bytes(),
                original_filename=fixture.pdf_filename,
            )
            extraction = extract(
                session, store, document, version, client=client, settings=settings
            )
            out[fixture.fixture_id] = score(
                fixture.fields, effective_values(session, extraction.id)
            )
    session.rollback()
    session.close()
    return out


def main(version_a: str, version_b: str) -> None:
    a, b = run_version(version_a), run_version(version_b)
    paths = {p for fields in a.values() for p in fields}
    print(f"{'field path':<44} {version_a:>8} {version_b:>8}   delta")
    for path in sorted(paths):
        a_vals = [f[path] for f in a.values() if path in f]
        b_vals = [f[path] for f in b.values() if path in f]
        a_pct = 100 * sum(a_vals) / len(a_vals) if a_vals else 0.0
        b_pct = 100 * sum(b_vals) / len(b_vals) if b_vals else 0.0
        if a_pct != b_pct:
            print(f"{path:<44} {a_pct:7.1f}% {b_pct:7.1f}%  {b_pct - a_pct:+6.1f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
