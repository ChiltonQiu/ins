"""Read-only .ics for subscription.

Subscribe only. Nothing is ever written into her calendar and there is no
two-way sync, so a mistake here can never corrupt the calendar she already
depends on.

An unconfirmed date is labelled as such in the summary. It leaves this system
into an app that knows nothing about confirmation state, and a wrong
cancellation date sitting unlabelled in her phone's calendar is exactly the
failure this design exists to prevent.
"""

from __future__ import annotations

from renewal.calendarview.agenda import AgendaEntry

# RFC 5545 escaping. Backslash first, or it would escape the backslashes the
# later rules introduce.
_ESCAPES = (("\\", "\\\\"), (";", "\\;"), (",", "\\,"), ("\n", "\\n"))


def _escape(value: str) -> str:
    for old, new in _ESCAPES:
        value = value.replace(old, new)
    return value


def render_ics(entries: list[AgendaEntry], *, calendar_name: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//renewal//record layer//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    for entry in entries:
        stamp = entry.date_value.strftime("%Y%m%d")
        who = entry.client_name or "Unmatched document"
        summary = f"{who} — {entry.title}"
        if entry.status == "unconfirmed":
            summary = f"[UNCONFIRMED] {summary}"
        if entry.is_derived:
            summary = f"{summary} (computed)"
        lines += [
            "BEGIN:VEVENT",
            # No DTSTAMP: it would change on every render, and her calendar app
            # relies on a stable body to update an event rather than duplicate
            # it.
            f"UID:{entry.kind}-{entry.source_id}@renewal",
            f"DTSTART;VALUE=DATE:{stamp}",
            f"SUMMARY:{_escape(summary)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
