"""Runs the current extractor against every labelled fixture.

Marked `eval` because it makes real API calls; excluded from the default run.
Run it with: pytest -m eval evals/test_extraction.py -s

Each run writes `<name>.latest.json` beside the baseline it compared against.
Promoting a run's results to the new baseline means copying that file over
`<name>.json`, so the provider/model/version digest in the filename is carried
along rather than retyped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from evals.accuracy import (
    accuracy,
    baseline_path,
    load_fixtures,
    regressions,
    report,
    score,
)
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.corrections import effective_values
from renewal.extract.runner import extract
from renewal.extract.validate import verification_rate
from renewal.ingest import ingest_pdf
from renewal.providers import build_client

FIXTURE_DIR = Path(__file__).parent / "fixtures"
PDF_DIR = Path(__file__).parent / "pdfs"
BASELINE_DIR = Path(__file__).parent / "baselines"
VERSION = "v1"


@pytest.mark.eval
def test_extractor_accuracy_against_fixtures(engine, tmp_path, capsys):
    fixtures = load_fixtures(FIXTURE_DIR)
    available = [f for f in fixtures if (PDF_DIR / f.pdf_filename).exists()]
    if not available:
        pytest.skip("no fixture PDFs present in evals/pdfs/")

    settings = load_settings()
    baseline = baseline_path(
        BASELINE_DIR, settings.provider, settings.extraction_model, VERSION
    )
    store = BlobStore(tmp_path / "blobs")
    client = build_client(settings)
    session = sessionmaker(bind=engine)()

    results: dict[str, dict] = {}
    for fixture in available:
        data = (PDF_DIR / fixture.pdf_filename).read_bytes()
        document = ingest_pdf(
            session, store, data=data, original_filename=fixture.pdf_filename
        )
        extraction = extract(
            session, store, document, VERSION, client=client, settings=settings
        )
        actual = effective_values(session, extraction.id)
        results[fixture.fixture_id] = {
            "carrier": fixture.carrier,
            "fields": score(fixture.fields, actual),
            "verification_rate": verification_rate(extraction.fields),
        }
    session.rollback()
    session.close()

    with capsys.disabled():
        print(f"\nprovider: {settings.provider}  model: {settings.extraction_model}")
        print(report(results))
        for fixture_id, entry in sorted(results.items()):
            rate = entry["verification_rate"]
            shown = "n/a" if rate is None else f"{100 * rate:5.1f}%"
            print(
                f"  {fixture_id:<28} {100 * accuracy(entry['fields']):5.1f}% "
                f"verified {shown}"
            )

    if baseline.exists():
        recorded = json.loads(baseline.read_text())
        broken = regressions(
            {k: v["fields"] for k, v in recorded.items()},
            {k: v["fields"] for k, v in results.items()},
        )
        assert not broken, f"fields that used to pass and now fail: {broken}"

    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.with_suffix(".latest.json").write_text(json.dumps(results, indent=2))
