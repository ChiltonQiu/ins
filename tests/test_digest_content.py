"""What one summary says.

Numbers, a date, and a link. No client name, no policy number, no filename —
the login exists to keep that off a mail server and a summary is not an
exception to it.
"""

import dataclasses
from datetime import date, timedelta

from renewal.digest.content import Digest, collect, lines, render, subject_for
from renewal.ingest import ingest_pdf
from renewal.models import Client, DocumentDate, DocumentLink
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


def _document(session, store, name="thing.pdf"):
    document = ingest_pdf(
        session, store, data=make_text_pdf([["unrecognisable"]]),
        original_filename=name, source="manual_upload", agency_id=1,
    )
    session.flush()
    return document


def _dated(session, store, when, *, name="dec.pdf"):
    document = _document(session, store, name)
    session.add(DocumentDate(
        document_id=document.id, date_value=when,
        date_type="policy_expiration", source_page=1, source_text="Expires",
        confidence=0.9, extractor_version="dates-regex-v1", pass_name="regex",
    ))
    session.flush()
    return document


def test_an_empty_system_is_quiet(session, store):
    digest = collect(session, settings=_settings(), today=date(2026, 9, 15))
    assert digest.is_quiet
    assert digest.quiet_days is None


def test_a_document_that_needs_her_is_counted(session, store):
    _document(session, store)
    digest = collect(session, settings=_settings(), today=date.today())
    assert digest.needs_you == 1
    assert not digest.is_quiet


def test_a_date_inside_the_window_is_counted_and_dated(session, store):
    today = date.today()
    _dated(session, store, today + timedelta(days=7))
    digest = collect(session, settings=_settings(), today=today)
    assert digest.upcoming == 1
    assert digest.soonest == today + timedelta(days=7)


def test_a_date_beyond_the_window_is_not(session, store):
    today = date.today()
    _dated(session, store, today + timedelta(days=90))
    digest = collect(session, settings=_settings(), today=today)
    assert digest.upcoming == 0
    assert digest.soonest is None


def test_the_window_is_her_window(session, store):
    """The same number the attention queue uses, so the email and the page
    cannot disagree about what 'soon' means."""
    today = date.today()
    _dated(session, store, today + timedelta(days=20))
    wide = dataclasses.replace(_settings(), unconfirmed_date_window_days=30)
    assert collect(session, settings=wide, today=today).upcoming == 1


def test_the_body_names_no_client_and_no_file(session, store):
    today = date.today()
    document = _dated(session, store, today + timedelta(days=3),
                      name="ramirez-landscaping-renewal.pdf")
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="manual", confidence=1.0))
    session.flush()

    _, body = render(collect(session, settings=_settings(), today=today),
                     settings=_settings())

    assert "Ramirez" not in body
    assert ".pdf" not in body
    assert body.count("http") == 1


def test_a_quiet_summary_says_so_plainly():
    """An email that looks like an alert and contains no alert teaches her to
    stop opening them."""
    quiet = Digest(needs_you=0, attention=0, upcoming=0, soonest=None,
                   quiet_days=9)
    subject, body = render(quiet, settings=_settings())
    assert subject == "Nothing needs you"
    assert "9 days" in body


def test_a_zero_is_left_out_rather_than_written():
    digest = Digest(needs_you=2, attention=0, upcoming=0, soonest=None,
                    quiet_days=0)
    written = lines(digest, settings=_settings())
    assert len(written) == 1
    assert "2 documents" in written[0]
    assert subject_for(digest) == "2 documents need you"
