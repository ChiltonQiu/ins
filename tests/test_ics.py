from datetime import date

from renewal.calendarview.agenda import AgendaEntry
from renewal.calendarview.ics import render_ics


def _entry(**kwargs):
    base = dict(kind="document_date", source_id=1, date_value=date(2026, 7, 1),
                date_type="policy_expiration", title="policy expiration",
                client_id=1, client_name="Acme Landscaping LLC", document_id=1,
                status="confirmed", confidence=0.9, is_derived=False,
                anchor_date=None, anchor_source_text=None, source_text="x",
                source_page=1, escalated=False)
    base.update(kwargs)
    return AgendaEntry(**base)


def test_renders_a_valid_calendar_envelope():
    body = render_ics([_entry()], calendar_name="Deadlines")
    assert body.startswith("BEGIN:VCALENDAR")
    assert body.rstrip().endswith("END:VCALENDAR")


def test_an_event_carries_the_client_and_the_type():
    body = render_ics([_entry()], calendar_name="Deadlines")
    assert "Acme Landscaping LLC" in body
    assert "policy expiration" in body


def test_an_unconfirmed_date_is_labelled_as_unconfirmed():
    """It must not read as fact in her calendar app either."""
    body = render_ics([_entry(status="unconfirmed")], calendar_name="Deadlines")
    assert "UNCONFIRMED" in body.upper()


def test_uids_are_stable_across_renders():
    """So her calendar app updates an event rather than duplicating it."""
    first = render_ics([_entry()], calendar_name="Deadlines")
    second = render_ics([_entry()], calendar_name="Deadlines")
    assert first == second


def test_document_and_manual_dates_get_distinct_uids():
    body = render_ics(
        [_entry(kind="document_date", source_id=1),
         _entry(kind="manual_date", source_id=1)],
        calendar_name="Deadlines",
    )
    assert body.count("UID:") == 2
    assert len(set(line for line in body.splitlines() if line.startswith("UID:"))) == 2


def test_special_characters_are_escaped():
    body = render_ics([_entry(client_name="Smith, Jones; & Co")],
                      calendar_name="Deadlines")
    assert "Smith\\, Jones\\; & Co" in body
