"""Date-extraction recall and precision over the labelled fixtures.

Marked `eval` because it makes real API calls. Run with:
    pytest -m eval evals/test_dates.py -s

Only recall regression fails the build. A missed date is the failure mode with
real consequences; a spurious one costs two seconds to dismiss.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from evals.accuracy import (
    baseline_path, date_report, load_fixtures, score_dates, score_match,
)
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.dates.service import extract_dates
from renewal.models import Client, DocumentText
from renewal.pdftext import PageText
from renewal.pipeline import ingest_document
from renewal.providers import build_client
from renewal.resolve.service import candidates_for

FIXTURE_DIR = Path(__file__).parent / "fixtures"
PDF_DIR = Path(__file__).parent / "pdfs"
BASELINE_DIR = Path(__file__).parent / "baselines"
VERSION = "dates-v1"


@pytest.mark.eval
def test_date_recall_against_fixtures(engine, tmp_path, capsys):
    fixtures = [f for f in load_fixtures(FIXTURE_DIR)
                if (PDF_DIR / f.pdf_filename).exists()]
    if not fixtures:
        pytest.skip("no fixture PDFs present in evals/pdfs/")

    settings = load_settings()
    client = build_client(settings)
    store = BlobStore(tmp_path / "blobs")
    session = sessionmaker(bind=engine)()
    results: dict[str, dict] = {}

    for fixture in fixtures:
        document = ingest_document(
            session, store, data=(PDF_DIR / fixture.pdf_filename).read_bytes(),
            original_filename=fixture.pdf_filename, source="manual_upload",
            agency_id=1, model_client=client, settings=settings,
        )
        pages = [
            PageText(row.page_number, row.text)
            for row in session.query(DocumentText)
            .filter_by(document_id=document.id)
            .order_by(DocumentText.page_number)
        ]
        rows = extract_dates(session, document, pages,
                             client=client, settings=settings)
        actual = [{"date_value": r.date_value.isoformat(),
                   "date_type": r.date_type} for r in rows]
        results[fixture.fixture_id] = {
            "carrier": fixture.carrier,
            "score": vars(
                score_dates(fixture.dates, actual,
                            billing_type=fixture.billing_type)
            ),
        }

        # Client resolution ships in this plan, so its scorer runs here rather
        # than waiting for the mail intake work.
        ranked = [m.client_id for m in candidates_for(session, document)]
        expected_id = None
        if fixture.expected_client:
            expected_id = session.scalar(
                select(Client.id).where(Client.display_name == fixture.expected_client)
            )
        results[fixture.fixture_id]["match"] = vars(score_match(expected_id, ranked))

    session.rollback()
    session.close()

    with capsys.disabled():
        print(date_report(results))

    baseline = baseline_path(BASELINE_DIR, settings.provider,
                             settings.date_model, VERSION)
    if baseline.exists():
        recorded = json.loads(baseline.read_text())
        for fixture_id, entry in recorded.items():
            was = entry["score"]["recall"]
            now = results.get(fixture_id, {}).get("score", {}).get("recall", 0.0)
            assert now >= was, (
                f"{fixture_id}: date recall regressed from {was:.2f} to {now:.2f}"
            )

    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.with_suffix(".latest.json").write_text(json.dumps(results, indent=2))
