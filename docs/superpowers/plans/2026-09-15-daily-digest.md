# The Daily Summary — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the application a clock of its own, so a deadline next Tuesday reaches her in a week when no mail arrives — and so a week with no mail is itself something she is told about.

**Architecture:** No scheduler and no second process. One function, `send_due_digest`, answers "given the state of the world right now, should she hear from me today?" entirely from the read-time functions the pages already call. A daemon thread in the application process asks it every five minutes, and the first successful send of the day writes a `notification_send` row whose `digest_date` stops every later tick. Because the answer is computed from state rather than from an event, a tick that never happened costs nothing: the next one asks the same question and gets the same answer.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, PostgreSQL, pytest, `smtplib`, `threading`, `zoneinfo`.

**Spec:** `docs/superpowers/specs/2026-09-15-daily-digest-design.md`

## Global Constraints

- **It fails toward noise, never toward silence.** The `notification_send` row is written *after* a successful send, never before. Nothing may claim a send that did not happen (`renewal/notify.py:86`), and no design in this plan trades a duplicate email for a silently missed day.
- **The email says numbers, a date, and a link.** No client names, no policy numbers, no document filenames, no subject lines from inbound mail. `renewal/notify.py:78` is unchanged law.
- **Nothing is sent to a client.** The only outbound mail is to the operator, as before.
- **Every number comes from the function the page calls.** `inbox.needs_you_count`, `attention.rules.open_items`, `calendarview.agenda.agenda`. No second implementation of any count, ever.
- **Her settings, not the environment's.** Anything that reads a preference reads it through `settings_store.effective(session, settings)`, the way `renewal/web/attention.py:41` and `renewal/background.py:96` already do.
- **A clock that dies must not die silently.** A tick that raises is logged and the loop continues.
- **`create_app` stays a pure function of its arguments.** The thread is started from `renewal/app.py`, the production wiring. No test application may raise a thread.
- Run the suite with `.venv/bin/pytest`. It needs PostgreSQL; override the database with `TEST_DATABASE_URL`.
- Baseline before this plan starts: **812 tests passing, 3 deselected.**

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `renewal/digest/__init__.py` | Empty. The package marker, as `renewal/attention/__init__.py` is. |
| `renewal/digest/content.py` | What a summary *is*: the counts, and the subject and body they render to. No I/O beyond reading. |
| `renewal/digest/send.py` | Whether to send one *now*, and the day-record that stops the next tick. |
| `renewal/digest/clock.py` | The thread that asks. One pass is `tick()`; the loop is `run()`. |
| `scripts/digest.py` | One tick from the command line, for an installation driving this from cron. |
| `migrations/versions/*_digest_date.py` | `notification_send.digest_date` and its partial unique index. |
| `tests/test_digest_content.py` | The counts and the wording. |
| `tests/test_digest_send.py` | The hour gate, the once-a-day gate, the quiet rule, the failure rule. |
| `tests/test_digest_clock.py` | `tick()` commits; `run()` survives a raising tick; `start()` is a daemon. |

**Modified:**

| File | Change |
|---|---|
| `renewal/models.py` | `NotificationSend.digest_date`, and the partial unique index below the class. |
| `renewal/config.py` | `agency_tz`, `digest_enabled`, `digest_hour`, `digest_quiet_days`, `digest_tick_seconds`. |
| `renewal/settings_store.py` | Three `Definition`s: `digest_enabled`, `digest_hour`, `digest_quiet_days`. |
| `renewal/notify.py` | The event email's body comes from `digest.content`, so both emails say the same things. |
| `renewal/app.py` | Start the clock. |
| `.env.example` | `AGENCY_TZ`, `DIGEST_TICK_SECONDS`. |
| `README.md` | A "Daily summary" section under Accounts. |
| `tests/test_notify.py` | The event email now carries the deadline line too. |

---

### Task 1: The day a summary was sent

The whole once-a-day guarantee rests on one column. A `notification_send` row
with `digest_date` set is a daily summary; a row with it NULL is the existing
event-triggered email. Postgres treats NULLs as distinct in a unique index, so
the two kinds live in one table without interfering — the same property
`uq_inbound_message_id` already depends on (`renewal/models.py:630`).

**Files:**
- Modify: `renewal/models.py:669-680`
- Create: `migrations/versions/<rev>_digest_date.py`
- Test: `tests/test_digest_day.py`

**Interfaces:**
- Produces: `NotificationSend.digest_date: Mapped[date | None]`, and the index
  `uq_notification_send_digest_date`, unique where `digest_date IS NOT NULL`.

- [x] **Step 1: Write the failing test**

Create `tests/test_digest_day.py`:

```python
"""One summary per local day, and the database is what remembers.

A row with digest_date set is a daily summary. A row with it NULL is the
event-triggered email, and there may be any number of those.
"""

import pytest
from datetime import date
from sqlalchemy.exc import IntegrityError

from renewal.models import NotificationSend


def test_two_summaries_for_one_day_cannot_both_exist(session):
    session.add(NotificationSend(document_count=1, digest_date=date(2026, 9, 15)))
    session.flush()
    session.add(NotificationSend(document_count=9, digest_date=date(2026, 9, 15)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_summaries_on_different_days_coexist(session):
    session.add(NotificationSend(document_count=1, digest_date=date(2026, 9, 15)))
    session.add(NotificationSend(document_count=1, digest_date=date(2026, 9, 16)))
    session.flush()
    assert session.query(NotificationSend).count() == 2


def test_the_event_email_is_unconstrained(session):
    """Its digest_date is NULL, and NULLs are distinct. A busy morning may
    send several without either of them being a summary."""
    for _ in range(3):
        session.add(NotificationSend(document_count=1))
    session.flush()
    assert session.query(NotificationSend).count() == 3
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_digest_day.py -q`
Expected: FAIL — `TypeError: 'digest_date' is an invalid keyword argument`.

- [x] **Step 3: Add the column and the index**

In `renewal/models.py`, replace the `NotificationSend` class body:

```python
class NotificationSend(Base):
    """One row per operator email sent.

    A row rather than a column on agency, because the count at the time is
    worth keeping: it is the only record of how big the backlog got, and the
    guard reads the timestamp off the newest row anyway.

    digest_date set means a daily summary, and it is what stops the next tick
    from sending a second one. NULL means the event-triggered email, of which
    a morning may hold several.
    """

    __tablename__ = "notification_send"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_count: Mapped[int] = mapped_column(Integer)
    digest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sent_at: Mapped[datetime] = _created_at()
```

Then, immediately after the class (a partial unique index cannot be spelled in
`__table_args__` without naming a column the class body has not defined yet —
the same reason `uq_app_user_email_lower` sits below `User` at
`renewal/models.py:751`):

```python
# One summary per day, and no constraint at all on the event-triggered email.
# A CHECK cannot express "unique among the non-NULLs"; a partial unique index
# can, and Postgres would treat the NULLs as distinct even without it.
Index(
    "uq_notification_send_digest_date",
    NotificationSend.__table__.c.digest_date,
    unique=True,
    postgresql_where=NotificationSend.__table__.c.digest_date.isnot(None),
)
```

`Date` and `Index` are already imported at `renewal/models.py:16-29`.

- [x] **Step 4: Write the migration**

```bash
.venv/bin/alembic revision -m "digest date"
```

Fill the generated file's `upgrade`/`downgrade` (leave the revision ids Alembic
generated; `down_revision` must already be `f3b96d41c8a2`):

```python
def upgrade() -> None:
    op.add_column(
        "notification_send", sa.Column("digest_date", sa.Date(), nullable=True)
    )
    # Every row written before this migration was the event-triggered email,
    # which is exactly what a NULL digest_date means. Nothing to backfill.
    op.create_index(
        "uq_notification_send_digest_date", "notification_send", ["digest_date"],
        unique=True, postgresql_where=sa.text("digest_date IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_notification_send_digest_date", table_name="notification_send"
    )
    op.drop_column("notification_send", "digest_date")
```

- [x] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_digest_day.py tests/test_notify.py -q`
Expected: PASS (3 new, 7 existing).

- [x] **Step 6: Commit**

```bash
git add renewal/models.py migrations/versions tests/test_digest_day.py
git commit -m "feat(digest): the day a summary was sent"
```

---

### Task 2: The hour, the quiet window, and the timezone

Three preferences and two deployment variables. The split is
`settings_store.py:9`: a judgment about how she wants to work goes on
`/settings`; a number set once against the machine goes in the environment.

`AGENCY_TZ` is the one that matters most and is easiest to miss. Every
timestamp in this application is UTC, so without it "eight in the morning" is
four in the morning on the east coast.

**Files:**
- Modify: `renewal/config.py:52-63`, `renewal/config.py:110-124`
- Modify: `renewal/settings_store.py:60-130`
- Modify: `.env.example`
- Test: `tests/test_config.py`, `tests/test_settings_store.py`

**Interfaces:**
- Produces: `Settings.agency_tz: str = "UTC"`, `Settings.digest_enabled: bool = True`,
  `Settings.digest_hour: int = 8`, `Settings.digest_quiet_days: int = 7`,
  `Settings.digest_tick_seconds: int = 300`. The first four are read by Tasks 3–5;
  the last only by `renewal/app.py` in Task 5.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_the_agency_timezone_comes_from_the_environment(monkeypatch):
    """Every timestamp here is UTC. Without this, an eight o'clock summary is
    sent at four in the morning on the east coast."""
    monkeypatch.setenv("AGENCY_TZ", "America/New_York")
    assert load_settings().agency_tz == "America/New_York"


def test_the_agency_timezone_defaults_to_utc(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("AGENCY_TZ", raising=False)
    assert load_settings().agency_tz == "UTC"


def test_the_tick_interval_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DIGEST_TICK_SECONDS", "60")
    assert load_settings().digest_tick_seconds == 60
```

Append to `tests/test_settings_store.py`:

```python
def test_the_digest_hour_is_a_preference_within_the_day(session):
    write(session, "digest_hour", "6")
    assert effective(session, _settings()).digest_hour == 6

    with pytest.raises(Invalid):
        write(session, "digest_hour", "24")


def test_the_quiet_window_is_a_preference(session):
    write(session, "digest_quiet_days", "3")
    assert effective(session, _settings()).digest_quiet_days == 3

    with pytest.raises(Invalid):
        write(session, "digest_quiet_days", "0")


def test_the_timezone_is_not_a_preference():
    """Deployment configuration: set once against the machine, never a
    judgment about insurance."""
    assert "agency_tz" not in BY_KEY
    assert "digest_tick_seconds" not in BY_KEY
```

`pytest` is already imported in `tests/test_settings_store.py`; confirm with
`head -12 tests/test_settings_store.py` and add `import pytest` if it is not.

- [x] **Step 2: Run them to make sure they fail**

Run: `.venv/bin/pytest tests/test_config.py tests/test_settings_store.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'agency_tz'`,
and `Invalid: no such setting: digest_hour`.

- [x] **Step 3: Add the fields**

In `renewal/config.py`, after `notify_min_interval_minutes` (line 52):

```python
    # The daily summary. Operator-editable; these are the floor a fresh
    # install starts from.
    digest_enabled: bool = True
    digest_hour: int = 8
    digest_quiet_days: int = 7
```

and in the deployment block after `intake_workers` (line 63):

```python
    # Everything else here is UTC. This is the one place a local hour is
    # meant, and getting it wrong sends the summary in the middle of the night
    # rather than not at all.
    agency_tz: str = "UTC"
    digest_tick_seconds: int = 300
```

In `load_settings()`, after the `unconfirmed_date_window_days` entry:

```python
        digest_enabled=os.environ.get(
            "DIGEST_ENABLED", "true"
        ).lower() not in ("0", "false", "no"),
        digest_hour=int(os.environ.get("DIGEST_HOUR", "8")),
        digest_quiet_days=int(os.environ.get("DIGEST_QUIET_DAYS", "7")),
        agency_tz=os.environ.get("AGENCY_TZ", "UTC"),
        digest_tick_seconds=int(os.environ.get("DIGEST_TICK_SECONDS", "300")),
```

- [x] **Step 4: Add the three definitions**

In `renewal/settings_store.py`, inside `DEFINITIONS`, after the
`notify_min_interval_minutes` entry:

```python
    Definition(
        key="digest_enabled",
        label="Email me a summary every day",
        help=(
            "Sent once a day whether or not anything arrived, which is the "
            "only thing here that speaks without being spoken to. Off means "
            "you hear from this application only when a document lands."
        ),
        kind="bool",
    ),
    Definition(
        key="digest_hour",
        label="Send that summary after",
        help=(
            "The hour of your own day, not the server's. If the application "
            "was not running at that hour, the summary goes out when it comes "
            "back — a late summary is worth more than none."
        ),
        kind="int",
        minimum=0,
        maximum=23,
        unit="o'clock",
    ),
    Definition(
        key="digest_quiet_days",
        label="Say so even when nothing needs me, every",
        help=(
            "When nothing is waiting, no summary is sent — until this many "
            "days have passed, and then one goes out saying exactly that. A "
            "quiet week and a broken mail hook look identical from here "
            "otherwise."
        ),
        kind="int",
        minimum=1,
        maximum=30,
        unit="days",
    ),
```

The settings page is data-driven from `DEFINITIONS`
(`renewal/templates/settings.html:32`), so these three appear on `/settings`
with no template change.

- [x] **Step 5: Document the two environment variables**

In `.env.example`, after the notification block:

```
# The daily summary's clock. AGENCY_TZ is the timezone whose eight o'clock is
# meant by DIGEST_HOUR on /settings — everything else in this application is
# UTC. DIGEST_TICK_SECONDS is how often the application asks whether the
# summary is due; 0 turns the in-process clock off entirely, for an install
# driving `python -m scripts.digest` from cron instead.
AGENCY_TZ=UTC
DIGEST_TICK_SECONDS=300
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py tests/test_settings_store.py tests/test_web_settings.py -q`
Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add renewal/config.py renewal/settings_store.py .env.example tests/test_config.py tests/test_settings_store.py
git commit -m "feat(digest): the hour, the quiet window, and the agency timezone"
```

---

### Task 3: What a summary says

The counts, and the words they render to. Nothing here decides whether to send
anything, and nothing here touches SMTP — which is what makes every line of it
testable without a mail server.

Every number comes from the function the corresponding page calls. That is not
tidiness: an email that disagrees with the screen she opens from its own link
is worse than no email, because she stops believing both.

**Files:**
- Create: `renewal/digest/__init__.py` (empty)
- Create: `renewal/digest/content.py`
- Test: `tests/test_digest_content.py`

**Interfaces:**
- Consumes: `Settings.digest_quiet_days` and `Settings.unconfirmed_date_window_days` from Task 2.
- Produces:
  - `Digest` — frozen dataclass with `needs_you: int`, `attention: int`,
    `upcoming: int`, `soonest: date | None`, `quiet_days: int | None`, and a
    property `is_quiet: bool`.
  - `collect(session, *, settings, today: date | None = None) -> Digest`
  - `lines(digest, *, settings) -> list[str]`
  - `subject_for(digest) -> str`
  - `render(digest, *, settings) -> tuple[str, str]` returning `(subject, body)`

- [x] **Step 1: Write the failing test**

Create `tests/test_digest_content.py`:

```python
"""What one summary says.

Numbers, a date, and a link. No client name, no policy number, no filename —
the login exists to keep that off a mail server and a summary is not an
exception to it.
"""

import dataclasses
from datetime import date, timedelta

from renewal.digest.content import collect, lines, render, subject_for
from renewal.ingest import ingest_pdf
from renewal.models import Client, DocumentDate, DocumentLink
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


def _document(session, store, name="thing.pdf"):
    return ingest_pdf(
        session, store, data=make_text_pdf([["unrecognisable"]]),
        original_filename=name, source="manual_upload", agency_id=1,
    )


def _dated(session, store, when, *, name="dec.pdf"):
    document = _document(session, store, name)
    session.add(DocumentDate(
        document_id=document.id, date_value=when,
        date_type="policy_expiration", confidence=0.9, source_page=1,
        source_text="Expiration Date", is_derived=False, **{"pass": "regex"},
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
    client = Client(display_name="Ramirez Landscaping", agency_id=1)
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
    from renewal.digest.content import Digest

    quiet = Digest(needs_you=0, attention=0, upcoming=0, soonest=None,
                   quiet_days=9)
    subject, body = render(quiet, settings=_settings())
    assert subject == "Nothing needs you"
    assert "9 days" in body


def test_a_zero_is_left_out_rather_than_written():
    from renewal.digest.content import Digest

    digest = Digest(needs_you=2, attention=0, upcoming=0, soonest=None,
                    quiet_days=0)
    written = lines(digest, settings=_settings())
    assert len(written) == 1
    assert "2 documents" in written[0]
    assert subject_for(digest) == "2 documents need you"
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_digest_content.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.digest'`.

- [x] **Step 3: Write the module**

```bash
mkdir -p renewal/digest && touch renewal/digest/__init__.py
```

Create `renewal/digest/content.py`:

```python
"""What one summary says.

Every number here comes from the function the corresponding page calls:
needs_you_count for the inbox, open_items for the attention queue, agenda for
the calendar. An email that disagrees with the screen she opens from its own
link is worse than no email, because she stops believing both.

Numbers, a date, and a link. Naming a client or a document here would put
client detail into a mailbox, which is exactly what the login exists to
prevent. A bare date carries no such detail and stays, because it is the one
thing this email exists to say.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.attention.rules import open_items
from renewal.calendarview.agenda import agenda
from renewal.config import Settings
from renewal.inbox import needs_you_count
from renewal.models import Document

# There is no tenancy yet; every screen is this one agency's, and so is this.
AGENCY_ID = 1


@dataclass(frozen=True)
class Digest:
    needs_you: int
    attention: int
    upcoming: int
    soonest: date | None
    # Days since anything last arrived, or None on an installation where
    # nothing ever has. This is the number that tells a quiet week apart from
    # a mail hook that has been failing since Thursday.
    quiet_days: int | None

    @property
    def is_quiet(self) -> bool:
        return not (self.needs_you or self.attention or self.upcoming)


def _quiet_days(session: Session, today: date) -> int | None:
    latest = session.scalar(select(func.max(Document.uploaded_at)))
    if latest is None:
        return None
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return (today - latest.astimezone(timezone.utc).date()).days


def collect(
    session: Session, *, settings: Settings, today: date | None = None
) -> Digest:
    """The state of the world, counted the way the pages count it."""
    today = today or date.today()
    window = settings.unconfirmed_date_window_days
    entries = agenda(
        session, agency_id=AGENCY_ID, start=today,
        end=today + timedelta(days=window),
    )
    return Digest(
        needs_you=needs_you_count(session, limit=settings.inbox_limit),
        attention=len(open_items(session, today=today, window_days=window)),
        upcoming=len(entries),
        soonest=min((entry.date_value for entry in entries), default=None),
        quiet_days=_quiet_days(session, today),
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def lines(digest: Digest, *, settings: Settings) -> list[str]:
    """One line per number that is not zero.

    A zero is left out rather than written as a zero: four lines of nothing
    are what an unread email looks like.
    """
    written: list[str] = []
    if digest.needs_you:
        written.append(
            f"{_plural(digest.needs_you, 'document')} in the inbox could not "
            "be filed without a person."
        )
    if digest.attention:
        written.append(
            f"{_plural(digest.attention, 'item')} in the attention queue."
        )
    if digest.upcoming and digest.soonest is not None:
        written.append(
            f"{_plural(digest.upcoming, 'date')} in the next "
            f"{settings.unconfirmed_date_window_days} days — the soonest is "
            f"{digest.soonest.strftime('%a %d %b')}."
        )
    return written


def subject_for(digest: Digest) -> str:
    """Number first, so the count is readable without opening anything."""
    if digest.is_quiet:
        return "Nothing needs you"
    if digest.needs_you:
        verb = "needs" if digest.needs_you == 1 else "need"
        return f"{_plural(digest.needs_you, 'document')} {verb} you"
    if digest.upcoming:
        return f"{_plural(digest.upcoming, 'date')} coming up"
    return f"{_plural(digest.attention, 'item')} in the attention queue"


def render(digest: Digest, *, settings: Settings) -> tuple[str, str]:
    written = lines(digest, settings=settings) or ["Nothing needs you."]
    # The arrival line is the whole point of a quiet summary and is worth
    # saying in a busy one too: three documents waiting and nothing new in
    # nine days is a forwarding rule that stopped, not a quiet week.
    if (
        digest.quiet_days is not None
        and digest.quiet_days >= settings.digest_quiet_days
    ):
        written.append(f"Nothing has arrived in {digest.quiet_days} days.")
    body = "\n".join(written) + f"\n\n{settings.base_url}/\n"
    return subject_for(digest), body
```

- [x] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_digest_content.py -q`
Expected: PASS (8 tests).

If `_dated`'s `DocumentDate(...)` keyword list does not match the model, read
`renewal/models.py` around `class DocumentDate` and fix the fixture — `pass` is
a reserved word there and must go through `**{"pass": "regex"}`.

- [x] **Step 5: Commit**

```bash
git add renewal/digest tests/test_digest_content.py
git commit -m "feat(digest): what one summary says"
```

---

### Task 4: Whether to send one now

The whole decision in one function, so there is one place to read when the
question is "why did she not get an email on Tuesday".

The order of the last two steps is the load-bearing part: the row is written
*after* the send, never before. A design that claims the day first would turn a
crash between claiming and sending into a silently missed day — which is the
bug this plan exists to fix. Written afterwards, the worst a race can do is
send twice, and `attention/rules.py:11` already settled which of those two is
the acceptable failure.

**Files:**
- Create: `renewal/digest/send.py`
- Test: `tests/test_digest_send.py`

**Interfaces:**
- Consumes: `collect`, `render` from Task 3; `NotificationSend.digest_date` from
  Task 1; `Settings.digest_enabled`, `digest_hour`, `digest_quiet_days`,
  `agency_tz` from Task 2; `renewal.notify.send_email` as the default sender.
- Produces: `send_due_digest(session, *, settings, now: datetime | None = None,
  send=None) -> bool`. Does not commit — the caller does, exactly as
  `maybe_notify` leaves the commit to `background.py:114`.

- [x] **Step 1: Write the failing test**

Create `tests/test_digest_send.py`:

```python
"""Whether a summary goes out now.

The four gates, in the order the function applies them: configured, the hour
has come, not already sent today, and either something is waiting or it has
been quiet long enough to be worth saying so.
"""

import dataclasses
from datetime import date, datetime, timedelta, timezone

from renewal.digest.send import send_due_digest
from renewal.ingest import ingest_pdf
from renewal.models import NotificationSend
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


def _configured(**overrides):
    base = {
        "smtp_host": "smtp.example.com",
        "notify_to": "her@agency.com",
        "notify_from": "app@agency.com",
        "agency_tz": "UTC",
        "digest_hour": 8,
        "digest_quiet_days": 7,
    }
    return dataclasses.replace(_settings(), **(base | overrides))


def _at(hour, day=15):
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)


def _waiting(session, store, n=1):
    for i in range(n):
        ingest_pdf(
            session, store, data=make_text_pdf([[f"unrecognisable {i}"]]),
            original_filename=f"{i}.pdf", source="manual_upload", agency_id=1,
        )
    session.flush()


def _sends(session, store, at, **overrides):
    sent = []
    result = send_due_digest(
        session, settings=_configured(**overrides), now=at,
        send=lambda subject, body, **k: sent.append((subject, body)),
    )
    return result, sent


def test_before_the_hour_nothing_goes_out(session, store):
    _waiting(session, store, 2)
    result, sent = _sends(session, store, _at(6))
    assert not result and sent == []


def test_after_the_hour_it_goes_out(session, store):
    _waiting(session, store, 2)
    result, sent = _sends(session, store, _at(9))
    assert result
    assert "2 documents" in sent[0][0]
    assert session.query(NotificationSend).one().digest_date == date(2026, 9, 15)


def test_a_second_tick_the_same_day_sends_nothing(session, store):
    """The clock asks every five minutes. It must not send every five
    minutes."""
    _waiting(session, store, 1)
    _sends(session, store, _at(8))
    result, sent = _sends(session, store, _at(13))
    assert not result and sent == []


def test_the_next_day_sends_again(session, store):
    _waiting(session, store, 1)
    _sends(session, store, _at(8))
    result, _ = _sends(session, store, _at(8, day=16))
    assert result


def test_a_process_that_was_down_at_eight_sends_when_it_comes_back(
    session, store
):
    """A late summary is worth more than none, and there is no event to have
    missed: the deadline is still next Tuesday."""
    _waiting(session, store, 1)
    result, _ = _sends(session, store, _at(23))
    assert result


def test_the_hour_is_local(session, store):
    """Eight in New York is noon UTC. At eleven UTC it is not yet time."""
    _waiting(session, store, 1)
    result, _ = _sends(session, store, _at(11), agency_tz="America/New_York")
    assert not result
    result, _ = _sends(session, store, _at(13), agency_tz="America/New_York")
    assert result


def test_a_quiet_day_sends_nothing(session, store):
    result, sent = _sends(session, store, _at(9))
    assert not result and sent == []


def test_quiet_for_long_enough_says_so(session, store):
    """A quiet week and a mail hook that has been failing since Thursday look
    identical from where she sits. This is the only thing that can tell her."""
    session.add(NotificationSend(document_count=0,
                                 digest_date=date(2026, 9, 1)))
    session.flush()
    result, sent = _sends(session, store, _at(9))
    assert result
    assert sent[0][0] == "Nothing needs you"


def test_quiet_but_recently_said_so_stays_quiet(session, store):
    session.add(NotificationSend(document_count=0,
                                 digest_date=date(2026, 9, 13)))
    session.flush()
    result, sent = _sends(session, store, _at(9))
    assert not result and sent == []


def test_notifications_off_means_no_summary(session, store):
    _waiting(session, store, 1)
    result, _ = _sends(session, store, _at(9), digest_enabled=False)
    assert not result
    result, _ = _sends(session, store, _at(9), notify_enabled=False)
    assert not result


def test_no_mail_server_means_no_summary(session, store):
    _waiting(session, store, 1)
    result, _ = _sends(session, store, _at(9), smtp_host="")
    assert not result
    assert session.query(NotificationSend).count() == 0


def test_a_failed_send_writes_no_row_and_retries(session, store):
    """Nothing may claim a send that did not happen. The next tick tries
    again rather than being locked out by a failure it had no part in."""
    _waiting(session, store, 1)

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    assert not send_due_digest(
        session, settings=_configured(), now=_at(9), send=boom
    )
    assert session.query(NotificationSend).count() == 0

    result, _ = _sends(session, store, _at(9, day=15))
    assert result


def test_a_broken_timezone_still_sends(session, store):
    """Fails toward noise. A typo in AGENCY_TZ sends at the wrong hour; it
    does not send never."""
    _waiting(session, store, 1)
    result, _ = _sends(session, store, _at(9), agency_tz="Mars/Olympus")
    assert result
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_digest_send.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.digest.send'`.

- [x] **Step 3: Write the module**

Create `renewal/digest/send.py`:

```python
"""Whether a summary goes out now.

The whole decision in one function, so there is one place to read when the
question is why she did not get an email on Tuesday.

The order of the last two steps is load-bearing. The row is written after the
send and never before: a design that claimed the day first would turn a crash
between claiming and sending into a silently missed day, which is the failure
this whole feature exists to remove. Written afterwards, the worst a race can
do is send two emails, and this application has always preferred a wrong flag
to a missed one.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.digest.content import collect, render
from renewal.models import NotificationSend
from renewal.notify import send_email

logger = logging.getLogger(__name__)


def _zone(settings: Settings) -> ZoneInfo:
    """A typo in AGENCY_TZ costs the right hour, never the summary."""
    try:
        return ZoneInfo(settings.agency_tz)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown AGENCY_TZ %r, using UTC", settings.agency_tz)
        return ZoneInfo("UTC")


def _already_sent(session: Session, today: date) -> bool:
    return session.scalar(
        select(NotificationSend.id)
        .where(NotificationSend.digest_date == today)
        .limit(1)
    ) is not None


def _last_summary(session: Session) -> date | None:
    return session.scalar(select(func.max(NotificationSend.digest_date)))


def send_due_digest(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
    send=None,
) -> bool:
    """Send today's summary if today's summary is due. Returns whether one
    went out. Does not commit — the caller does."""
    # Both switches, because notify_enabled is how she turns this application's
    # mail off altogether and a summary is not an exception to it.
    if not (settings.digest_enabled and settings.notify_enabled):
        return False
    if not settings.smtp_host or not settings.notify_to:
        return False

    now = now.astimezone(_zone(settings)) if now else datetime.now(_zone(settings))
    today = now.date()

    if now.hour < settings.digest_hour:
        return False
    if _already_sent(session, today):
        return False

    digest = collect(session, settings=settings, today=today)
    if digest.is_quiet:
        last = _last_summary(session)
        if last is not None and (today - last).days < settings.digest_quiet_days:
            return False

    subject, body = render(digest, settings=settings)
    try:
        (send or send_email)(subject, body, settings=settings)
    except Exception:  # noqa: BLE001 - a summary must not cost anything
        logger.exception("digest send failed date=%s", today)
        # No row: nothing was sent, so nothing may claim it was, and the next
        # tick tries again rather than being locked out by a failure it had no
        # part in.
        return False

    session.add(
        NotificationSend(document_count=digest.needs_you, digest_date=today)
    )
    session.flush()
    logger.info("digest sent date=%s needs_you=%s upcoming=%s",
                today, digest.needs_you, digest.upcoming)
    return True
```

- [x] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_digest_send.py -q`
Expected: PASS (13 tests).

- [x] **Step 5: Commit**

```bash
git add renewal/digest/send.py tests/test_digest_send.py
git commit -m "feat(digest): whether a summary goes out now"
```

---

### Task 5: The clock

A daemon thread that asks the question every five minutes, and asks it once on
startup. Same size as `renewal/background.py`: no worker, no queue, nothing new
to deploy.

`tick()` is one pass and is what the tests drive. `sleep` is injected, because
a test that sleeps to prove a loop ticked is a test that fails on a slow
machine.

It is started from `renewal/app.py` rather than `create_app`, so the hundred
applications the test suite builds raise no threads and the factory stays a
pure function of its arguments.

**Files:**
- Create: `renewal/digest/clock.py`
- Create: `scripts/digest.py`
- Modify: `renewal/app.py`
- Test: `tests/test_digest_clock.py`

**Interfaces:**
- Consumes: `send_due_digest` from Task 4, `settings_store.effective`,
  `Settings.digest_tick_seconds` from Task 2.
- Produces: `DigestClock(session_factory, settings, *, tick_seconds=300,
  sleep=time.sleep, send=None)` with `tick() -> bool`, `run() -> None`,
  `start() -> threading.Thread`.

- [x] **Step 1: Write the failing test**

Create `tests/test_digest_clock.py`:

```python
"""The clock.

It asks the question; send.py answers it. What is tested here is that it keeps
asking — a clock that dies of one bad night's data is silent forever after,
which is the bug this whole feature exists to fix.
"""

import dataclasses
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from renewal.digest.clock import DigestClock
from renewal.ingest import ingest_pdf
from renewal.models import NotificationSend
from renewal.settings_store import write
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


class _Stop(Exception):
    pass


def _configured(**overrides):
    base = {
        "smtp_host": "smtp.example.com",
        "notify_to": "her@agency.com",
        "notify_from": "app@agency.com",
        "agency_tz": "UTC",
        "digest_hour": 0,
    }
    return dataclasses.replace(_settings(), **(base | overrides))


def _clock(engine, sent, **kwargs):
    return DigestClock(
        sessionmaker(bind=engine), _configured(),
        send=lambda subject, body, **k: sent.append(subject), **kwargs,
    )


def test_a_tick_sends_and_commits(engine, clean_db, store):
    """It commits: a summary whose row is rolled back is a summary that goes
    out again five minutes later, and every five minutes after that."""
    factory = sessionmaker(bind=engine)
    with factory() as session:
        ingest_pdf(
            session, store, data=make_text_pdf([["unrecognisable"]]),
            original_filename="a.pdf", source="manual_upload", agency_id=1,
        )
        session.commit()

    sent = []
    assert _clock(engine, sent).tick()
    assert len(sent) == 1

    with factory() as session:
        assert session.query(NotificationSend).count() == 1
    # And the row it committed is what stops the next tick.
    assert not _clock(engine, sent).tick()
    assert len(sent) == 1


def test_a_tick_reads_her_settings_not_the_environment(engine, clean_db, store):
    """digest_enabled is a preference. Turning it off on /settings has to
    reach the thread, which never sees a request."""
    factory = sessionmaker(bind=engine)
    with factory() as session:
        ingest_pdf(
            session, store, data=make_text_pdf([["unrecognisable"]]),
            original_filename="a.pdf", source="manual_upload", agency_id=1,
        )
        write(session, "digest_enabled", "")
        session.commit()

    sent = []
    assert not _clock(engine, sent).tick()
    assert sent == []


def test_the_loop_keeps_going_after_a_tick_raises(engine, clean_db):
    """One bad night's data must not end the clock. A clock that dies of a
    single exception is silent every day after it, which is the bug."""
    ticks = []

    def boom():
        ticks.append(1)
        raise RuntimeError("the database went away")

    def sleeper(seconds):
        # Ends the loop once three ticks have been survived. Raising out of
        # sleep rather than out of tick is what makes this test about the
        # loop's tolerance rather than about its stopping condition.
        if len(ticks) >= 3:
            raise _Stop

    clock = _clock(engine, [], tick_seconds=0, sleep=sleeper)
    clock.tick = boom

    with pytest.raises(_Stop):
        clock.run()
    assert len(ticks) == 3


def test_the_thread_is_a_daemon(engine, clean_db):
    """It must never hold a shutdown open. Work in this process dies with
    this process, the same contract background.py states."""
    started = threading.Event()

    def sleeper(seconds):
        started.set()
        raise _Stop

    clock = _clock(engine, [], tick_seconds=0, sleep=sleeper)
    thread = clock.start()
    assert thread.daemon
    assert started.wait(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_digest_clock.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.digest.clock'`.

- [x] **Step 3: Write the clock**

Create `renewal/digest/clock.py`:

```python
"""The clock that asks whether a summary is due.

A daemon thread inside the application, which is the same size as the intake
runner beside it: no worker process, no queue, nothing new to deploy or
monitor.

It has the same one failure mode, stated the same way: work in this process
dies with this process. A day the application is down for its entirety is a day
with no summary. That is survivable only because the answer is computed from
state rather than from an event — the next day's summary names the same
deadline, because the deadline is still there.

sleep is injected so that a test can prove the loop ticks without sleeping.
A test that sleeps to win a race is a test that fails on a slow machine.
"""

from __future__ import annotations

import logging
import threading
import time

from renewal.config import Settings
from renewal.digest.send import send_due_digest
from renewal.settings_store import effective

logger = logging.getLogger(__name__)


class DigestClock:
    def __init__(
        self,
        session_factory,
        settings: Settings,
        *,
        tick_seconds: int = 300,
        sleep=time.sleep,
        send=None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._tick_seconds = tick_seconds
        self._sleep = sleep
        self._send = send

    def tick(self) -> bool:
        """One pass. Its own session, and it commits: a summary whose row is
        rolled back is a summary that goes out again five minutes later."""
        with self._session_factory() as session:
            # Her settings, not the environment's. The thread never sees a
            # request, so this is the only place they can reach it.
            settings = effective(session, self._settings)
            sent = send_due_digest(session, settings=settings, send=self._send)
            session.commit()
            return sent

    def run(self) -> None:
        """Ticks once before sleeping, so a process that comes up at two in
        the afternoon sends the morning's summary at two in the afternoon."""
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a clock must outlive one bad night
                logger.exception("digest tick failed")
            self._sleep(self._tick_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="digest", daemon=True)
        thread.start()
        return thread
```

- [x] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_digest_clock.py -q`
Expected: PASS (4 tests).

- [x] **Step 5: Start it in the production wiring**

Replace `renewal/app.py` entirely:

```python
"""Production wiring for uvicorn.

The clock is started here rather than inside create_app, because create_app is
called a hundred times by the test suite and none of those applications should
raise a thread. DIGEST_TICK_SECONDS=0 turns it off, for an installation driving
`python -m scripts.digest` from cron instead.
"""

from sqlalchemy.orm import sessionmaker

from renewal.blobstore import store_from_settings
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.digest.clock import DigestClock
from renewal.providers import build_client
from renewal.web import create_app

_settings = load_settings()
_session_factory = sessionmaker(bind=get_engine())

app = create_app(
    settings=_settings,
    store=store_from_settings(_settings),
    model_client=build_client(_settings),
    session_factory=_session_factory,
)

if _settings.digest_tick_seconds > 0:
    DigestClock(
        _session_factory, _settings,
        tick_seconds=_settings.digest_tick_seconds,
    ).start()
```

- [x] **Step 6: Add the command-line tick**

Create `scripts/digest.py`:

```python
"""One tick of the daily summary, then exit.

For an installation that would rather drive this from cron or a systemd timer
than from the application's own thread. Same code and the same once-a-day
guarantee: the notification_send row is what remembers, so a cron entry and a
running application cannot between them send two.

    */5 * * * * cd /srv/renewal && .venv/bin/python -m scripts.digest

Set DIGEST_TICK_SECONDS=0 if you do this, or the application's own clock is
doing the same work.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from renewal.config import load_settings
from renewal.db import get_engine
from renewal.digest.clock import DigestClock


def main() -> int:
    settings = load_settings()
    clock = DigestClock(sessionmaker(bind=get_engine()), settings)
    print("sent" if clock.tick() else "nothing due")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 7: Verify the application still imports and the suite is green**

Run: `.venv/bin/python -c "import renewal.app"` — expected: no output, exit 0.
Run: `.venv/bin/pytest -q` — expected: PASS, 812 + the new tests.

- [x] **Step 8: Commit**

```bash
git add renewal/digest/clock.py renewal/app.py scripts/digest.py tests/test_digest_clock.py
git commit -m "feat(digest): a clock of its own"
```

---

### Task 6: One body for both emails, and the README

The event-triggered email and the daily summary are one question asked by two
clocks, not two notification systems. Sharing the body is what keeps them from
drifting — and it closes most of this gap even in the weeks when mail *does*
arrive, because the email that fires when a document finishes processing gains
the deadline line for free.

`maybe_notify` keeps its own subject, its own once-an-hour guard, and its own
NULL `digest_date`. Only the body changes.

**Files:**
- Modify: `renewal/notify.py:49-98`
- Modify: `tests/test_notify.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `lines` and `collect` from Task 3.
- Produces: nothing new. `maybe_notify`'s signature is unchanged.

- [x] **Step 1: Write the failing test**

Append to `tests/test_notify.py`:

```python
def test_the_event_email_carries_the_deadline_too(session, store):
    """The same body the daily summary uses. A document arriving is a good
    moment to mention that something is due on Tuesday."""
    from datetime import date, timedelta

    from renewal.models import DocumentDate

    document = _waiting_document(session, store)
    session.add(DocumentDate(
        document_id=document.id, date_value=date.today() + timedelta(days=5),
        date_type="policy_expiration", confidence=0.9, source_page=1,
        source_text="Expiration Date", is_derived=False, **{"pass": "regex"},
    ))
    session.flush()

    sent = []
    maybe_notify(
        session, settings=_settings_with_mail(),
        send=lambda subject, body, **k: sent.append(body),
    )
    assert "date" in sent[0]
    assert "soonest" in sent[0]
```

and add the helper `_waiting_document` beside `_waiting` in that file:

```python
def _waiting_document(session, store, name="waiting.pdf"):
    document = ingest_pdf(
        session, store, data=make_text_pdf([["unrecognisable"]]),
        original_filename=name, source="manual_upload", agency_id=1,
    )
    session.flush()
    return document
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_notify.py -q`
Expected: FAIL — `assert "date" in sent[0]`, because the body is still the two
lines `maybe_notify` writes itself.

- [x] **Step 3: Share the body**

In `renewal/notify.py`, replace the module docstring's last paragraph and the
body-building block of `maybe_notify`.

Add to the imports:

```python
from renewal.digest.content import collect, lines
```

Replace the count and the body:

```python
    digest = collect(session, settings=settings)
    count = digest.needs_you
    if count == 0:
        return False
```

(keeping the `_last_send` guard between them exactly as it is), and:

```python
    subject = (
        f"{count} document{'' if count == 1 else 's'} need"
        f"{'s' if count == 1 else ''} you"
    )
    # The same lines the daily summary sends, so the two cannot drift into
    # disagreeing. Still a number and a link: naming the documents here would
    # put client detail into a mailbox, which is what the login prevents.
    body = "\n".join(lines(digest, settings=settings)) + f"\n\n{settings.base_url}/\n"
```

Delete the now-unused `from renewal.inbox import needs_you_count` import if
nothing else in the file uses it.

Update the module docstring's third paragraph, which is now half true:

```
No scheduler here. The background task that produced the backlog is what
notices it — this is the event-triggered half. The clock that speaks in a week
when nothing arrives is renewal/digest/clock.py, and both send the same body.
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_notify.py tests/test_digest_content.py tests/test_background.py -q`
Expected: PASS. If `test_the_email_carries_no_client_or_policy_detail` fails on
`sent[0].count("http") == 1`, the body has gained a second URL — it must not.

- [x] **Step 5: Document it**

In `README.md`, add a section after "Accounts":

```markdown
## The daily summary

Everything else here happens because a document arrived. This is the one thing
that happens because a day passed.

Once a day, after `Send that summary after` on `/settings`, one email goes out
saying how many documents need a person, how many items are in the attention
queue, and how many dates fall inside the window — with the nearest one named.
Numbers, a date and a link; no client names, no filenames, nothing that would
put client detail on a mail server.

When nothing is waiting, nothing is sent — until `digest_quiet_days` have
passed with no summary at all, and then one goes out saying exactly that. A
quiet week and a forwarding rule that broke on Thursday are otherwise the same
thing from where you sit.

Two variables set the clock, and both are deployment configuration rather than
preferences:

| Variable | Default | Meaning |
|---|---|---|
| `AGENCY_TZ` | `UTC` | Whose eight o'clock `Send that summary after` means. Everything else in this application is UTC, so leaving this unset sends the summary at four in the morning on the east coast. |
| `DIGEST_TICK_SECONDS` | `300` | How often the application asks whether the summary is due. `0` turns the in-process clock off. |

The clock is a thread inside the application, so it has the same failure mode
the background intake does: it dies with the process. A day the application is
down for its entirety is a day with no summary — but nothing is lost, because
the summary is computed from what is true rather than from events that might
have been missed. The next one names the same deadline.

To run it from cron instead, set `DIGEST_TICK_SECONDS=0` and add:

```bash
*/5 * * * * cd /srv/renewal && .venv/bin/python -m scripts.digest
```

The once-a-day record lives in the database, so a cron entry and a running
application cannot between them send two.
```

- [x] **Step 6: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: PASS. 812 baseline + roughly 30 new.

- [x] **Step 7: Commit**

```bash
git add renewal/notify.py tests/test_notify.py README.md
git commit -m "feat(digest): one body for both emails"
```

---

## Verification

After Task 6, before calling this done:

- [x] `.venv/bin/pytest -q` — green, and the count is the baseline plus the new tests.
- [x] `.venv/bin/alembic upgrade head` against the dev database, then
      `.venv/bin/alembic downgrade -1` and `upgrade head` again — the migration
      is reversible.
- [x] `/settings` renders the three new preferences and saving them sticks.
- [x] With `SMTP_HOST` unset, a tick does nothing and logs nothing alarming:
      `.venv/bin/python -m scripts.digest` prints `nothing due`.
