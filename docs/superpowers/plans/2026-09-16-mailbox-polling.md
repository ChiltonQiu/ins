# Polling a Mailbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull mail and its attachments out of a real mailbox on a schedule, so intake happens with nobody dropping a file into a form — using the intake path that already exists rather than a second one.

**Architecture:** A read-only IMAP connection produces raw bytes; `parse_mime` turns them into the `InboundEmail` the webhook already produces; `receive()` does the rest unchanged. A high-water UID per folder makes each poll cheap, and the existing per-agency unique `message_id` keeps it correct. A daemon thread on the digest clock's pattern runs it.

**Tech Stack:** Python 3.14 (`imaplib`, `email` — both standard library, no new dependency), SQLAlchemy 2.0, Alembic, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-mailbox-polling-design.md`

## Global Constraints

- **The mailbox is opened read-only, always.** `EXAMINE`, never `SELECT`. No task in this plan marks, moves, flags or deletes a message, and the connection is opened so the server refuses it.
- **One intake, two transports.** Nothing about parsing, dedupe, quarantine, body handling or pipeline dispatch is duplicated. Everything goes through `renewal/mail/intake.receive`.
- **The webhook's behaviour does not change.** It keeps routing by recipient. Only the poller passes an agency explicitly.
- **Correctness rests on Message-ID, never on a UID.** The high-water mark is an optimisation; `InboundMessage`'s unique `(agency_id, message_id)` is the guarantee.
- **Credentials never reach the database or a page.** Environment only, like `SMTP_*`.
- **One bad message never stops a poll**, and never gets retried forever.
- **No new dependency.** `imaplib` and `email` are standard library.
- Run the suite with `.venv/bin/pytest`. It needs PostgreSQL; override with `TEST_DATABASE_URL`.
- Baseline before this plan starts: **866 tests passing, 3 deselected.**

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `renewal/mail/imapbox.py` | The connection. Raw bytes in and out; knows nothing about documents. |
| `renewal/mail/poll.py` | One poll: what is new, hand each to intake, remember how far we got. |
| `renewal/mail/clock.py` | The thread that calls it. |
| `scripts/poll_mail.py` | One poll from the command line, for cron. |
| `migrations/versions/*_mail_poll_state.py` | `mail_poll_state`. |
| `tests/test_imapbox.py` | The connection, against a fake IMAP4 server object. |
| `tests/test_mail_poll.py` | New mail, duplicates, malformed mail, UIDVALIDITY reset. |

**Modified:**

| File | Change |
|---|---|
| `renewal/models.py` | `MailPollState`. |
| `renewal/mail/intake.py` | `receive(..., agency=None)`. |
| `renewal/config.py` | The six `IMAP_*` settings. |
| `renewal/app.py` | Start the poll clock. |
| `renewal/web/settings.py`, `renewal/templates/settings.html` | Last poll, what it found, last error. |
| `.env.example`, `README.md` | The variables and how to get an app password. |
| `conftest.py` | `mail_poll_state` in TABLES. |

---

### Task 1: Where the poll got to

One row per `(host, folder)`. Not `agency_setting`: that table is the
operator's preferences behind a whitelist, and a UID high-water mark is neither
a preference nor hers.

**Files:**
- Modify: `renewal/models.py`, `conftest.py`
- Create: `migrations/versions/<rev>_mail_poll_state.py`
- Test: `tests/test_mail_poll.py`

**Interfaces:**
- Produces: `MailPollState` with `host`, `folder`, `uid_validity: int | None`,
  `last_uid: int | None`, `last_polled_at`, `last_error: str | None`,
  `last_seen: int`, `last_ingested: int`, and a unique `(host, folder)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mail_poll.py`:

```python
"""Pulling mail out of a mailbox.

Nothing here talks to a real server: Mailbox is handed a fake IMAP4 object, so
what is tested is the sequencing, the dedupe and the failure handling rather
than somebody's TLS stack.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import MailPollState


def test_one_row_per_host_and_folder(session):
    session.add(MailPollState(host="imap.example.com", folder="Carriers"))
    session.flush()
    session.add(MailPollState(host="imap.example.com", folder="Carriers"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_same_folder_on_two_hosts_is_two_rows(session):
    session.add(MailPollState(host="imap.example.com", folder="INBOX"))
    session.add(MailPollState(host="imap.other.com", folder="INBOX"))
    session.flush()
    assert session.query(MailPollState).count() == 2


def test_a_fresh_row_has_seen_nothing(session):
    row = MailPollState(host="imap.example.com", folder="INBOX")
    session.add(row)
    session.flush()
    assert row.last_uid is None
    assert row.uid_validity is None
    assert row.last_error is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_mail_poll.py -q`
Expected: FAIL — `ImportError: cannot import name 'MailPollState'`.

- [ ] **Step 3: Add the model**

In `renewal/models.py`, after `InboundMessage`:

```python
class MailPollState(Base):
    """How far the poller has read, per mailbox folder.

    An optimisation and nothing more. Correctness lives on
    InboundMessage.message_id, which is unique per agency: losing this row
    means the next poll re-reads the folder and dedupes, which is slow and
    right rather than fast and wrong.
    """

    __tablename__ = "mail_poll_state"
    __table_args__ = (
        UniqueConstraint("host", "folder", name="uq_mail_poll_state_folder"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(Text)
    # NULL until the first successful poll. A UIDVALIDITY that no longer
    # matches means the folder was rebuilt and every UID we remember is a
    # number about a folder that no longer exists.
    uid_validity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What the last attempt did, so the settings page can say so. An intake
    # that has been broken since Thursday must not be invisible.
    last_seen: Mapped[int] = mapped_column(Integer, server_default="0")
    last_ingested: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Write the migration**

```bash
.venv/bin/alembic revision -m "mail poll state"
```

```python
def upgrade() -> None:
    op.create_table(
        "mail_poll_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("folder", sa.Text(), nullable=False),
        sa.Column("uid_validity", sa.Integer(), nullable=True),
        sa.Column("last_uid", sa.Integer(), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_ingested", sa.Integer(), server_default="0",
                  nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host", "folder", name="uq_mail_poll_state_folder"),
    )


def downgrade() -> None:
    op.drop_table("mail_poll_state")
```

Add `mail_poll_state` to the `TABLES` string in `conftest.py`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_mail_poll.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add renewal/models.py migrations/versions conftest.py tests/test_mail_poll.py
git commit -m "feat(mail): where the poll got to"
```

---

### Task 2: The poller says whose mail this is

`receive()` routes by recipient, which is right for a public webhook and wrong
for a mailbox we logged into. The intended workflow is that carrier mail is
forwarded into a folder, and a forwarded message keeps the original `To:` — so
recipient matching would quarantine every message.

**Files:**
- Modify: `renewal/mail/intake.py:52-75`
- Test: `tests/test_mail_intake.py`

**Interfaces:**
- Produces: `receive(session, store, email, *, client, settings, on_document=None, agency=None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mail_intake.py`:

```python
def test_a_named_agency_overrides_the_recipient(session, store):
    """A polled mailbox is its own routing: we logged into this account, so
    whose mail it is was settled before the envelope was read. Forwarded mail
    keeps the sender's original To:, which matches nothing."""
    from renewal.models import Agency, InboundMessage

    agency = session.query(Agency).first()
    email = _email(to_address="someone.else@elsewhere.example")

    message = receive(session, store, email, client=None, settings=_settings(),
                      agency=agency)

    assert message.processing_status == "processed"
    assert message.agency_id == agency.id


def test_without_a_named_agency_the_recipient_still_decides(session, store):
    """The webhook's behaviour is unchanged: it is a public door and the
    envelope is the only evidence of who the mail was for."""
    email = _email(to_address="someone.else@elsewhere.example")

    message = receive(session, store, email, client=None, settings=_settings())

    assert message.processing_status == "quarantined"
```

Read the top of `tests/test_mail_intake.py` first and reuse its existing
helpers for building an `InboundEmail` and a `Settings`; the names above
(`_email`, `_settings`) are placeholders for whatever that file already calls
them. If it has no recipient-overriding helper, pass `to_address` through the
one it has.

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_mail_intake.py -q`
Expected: FAIL — `TypeError: receive() got an unexpected keyword argument 'agency'`.

- [ ] **Step 3: Add the parameter**

In `renewal/mail/intake.py`, extend the signature:

```python
def receive(
    session: Session,
    store: BlobStore,
    email: InboundEmail,
    *,
    client: ModelClient,
    settings: Settings,
    on_document=None,
    agency: Agency | None = None,
) -> InboundMessage:
```

and replace the routing line:

```python
    raw_digest = store.put(email.raw_mime, ext="eml")
    # Routing by recipient is for the webhook, which is a public door: the
    # envelope is the only evidence there of who the mail was meant for. A
    # caller that already knows — the poller, which logged into this mailbox
    # — says so, because forwarded mail keeps the original To: and would
    # quarantine every message otherwise.
    agency = agency or agency_for(session, email.to_address)
```

Update the module docstring's first paragraph to say routing is by recipient
*unless the caller names an agency*, and why.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mail_intake.py tests/test_web_mail.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add renewal/mail/intake.py tests/test_mail_intake.py
git commit -m "feat(mail): a caller that knows whose mailbox this is can say so"
```

---

### Task 3: The connection

Raw bytes in and out. It knows nothing about documents, which is what keeps it
testable against a fake without a TLS stack or a server.

**Files:**
- Create: `renewal/mail/imapbox.py`
- Test: `tests/test_imapbox.py`

**Interfaces:**
- Produces: `Mailbox(host, user, password, *, folder="INBOX", port=993, connector=None)`
  as a context manager, with `uid_validity() -> int`,
  `uids_since(uid: int | None) -> list[int]`, and `fetch(uid: int) -> bytes`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_imapbox.py`:

```python
"""The IMAP connection, against a fake server.

Read-only is the property worth testing here: this connects to somebody's real
mail, and the guarantee that it cannot alter it has to be a test rather than a
comment.
"""

import pytest

from renewal.mail.imapbox import Mailbox


class FakeIMAP:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.validity = b"12345"

    def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b""]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def examine(self, folder):
        self.calls.append(("examine", folder))
        return "OK", [b"1"]

    def status(self, folder, what):
        return "OK", [b'"%s" (UIDVALIDITY %s)' % (folder.encode(), self.validity)]

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        if command == "FETCH":
            uid = int(args[0])
            return "OK", [(b"1 (RFC822 {%d}" % len(self.messages[uid]),
                           self.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]


def _box(fake, **kwargs):
    return Mailbox("imap.example.com", "anne", "app-password",
                   folder="Carriers", connector=lambda host, port: fake,
                   **kwargs)


def test_it_opens_the_folder_read_only():
    """EXAMINE, never SELECT. The server itself then refuses a write, so this
    cannot mark, move or delete anybody's mail even by accident."""
    fake = FakeIMAP()
    with _box(fake):
        pass

    assert ("examine", "Carriers") in fake.calls
    assert not any(call[0] == "select" for call in fake.calls)


def test_it_logs_out_even_when_the_body_raises():
    fake = FakeIMAP()
    with pytest.raises(RuntimeError):
        with _box(fake):
            raise RuntimeError("boom")

    assert ("logout",) in fake.calls


def test_uids_since_asks_only_for_what_is_new():
    fake = FakeIMAP()
    fake.messages = {4: b"a", 5: b"b"}
    with _box(fake) as box:
        assert box.uids_since(3) == [4, 5]

    assert any(call[0] == "uid" and call[1] == "SEARCH" and "4:*" in str(call)
               for call in fake.calls)


def test_uids_since_nothing_reads_the_whole_folder():
    fake = FakeIMAP()
    fake.messages = {1: b"a", 2: b"b"}
    with _box(fake) as box:
        assert box.uids_since(None) == [1, 2]


def test_fetch_returns_the_raw_message():
    fake = FakeIMAP()
    fake.messages = {7: b"From: a@b\r\n\r\nhello"}
    with _box(fake) as box:
        assert box.fetch(7) == b"From: a@b\r\n\r\nhello"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_imapbox.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.mail.imapbox'`.

- [ ] **Step 3: Write the module**

Create `renewal/mail/imapbox.py`:

```python
"""The IMAP connection.

Raw bytes out, nothing else. It knows about mailboxes and UIDs; it knows
nothing about documents, agencies or the pipeline, which is what lets a test
drive it with a fake object instead of a TLS stack.

Read-only on purpose, and structurally rather than by intention: the folder is
opened with EXAMINE, so the server refuses any write this code could issue.
It is reading somebody's actual mail, and the guarantee that it cannot damage
it should not rest on nobody ever making a mistake here.
"""

from __future__ import annotations

import imaplib
import logging
import re

logger = logging.getLogger(__name__)

_UIDVALIDITY = re.compile(rb"UIDVALIDITY\s+(\d+)")


class MailboxError(RuntimeError):
    """The mailbox refused something. Carries the server's own words."""


def _connect(host: str, port: int):
    return imaplib.IMAP4_SSL(host, port)


class Mailbox:
    def __init__(
        self, host: str, user: str, password: str, *, folder: str = "INBOX",
        port: int = 993, connector=_connect,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._folder = folder
        self._connector = connector
        self._imap = None

    def __enter__(self) -> "Mailbox":
        self._imap = self._connector(self._host, self._port)
        self._check(self._imap.login(self._user, self._password), "login")
        # EXAMINE rather than SELECT: read-only at the protocol level.
        self._check(self._imap.examine(self._folder), f"examine {self._folder}")
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            self._imap.logout()
        except Exception:  # noqa: BLE001 - a failed logout is not the caller's problem
            logger.warning("imap logout failed host=%s", self._host)
        self._imap = None

    @staticmethod
    def _check(response, what: str):
        status, data = response
        if status != "OK":
            raise MailboxError(f"{what}: {status} {data!r}")
        return data

    def uid_validity(self) -> int:
        """The folder's generation number. If this changes, every UID we
        remember refers to a folder that no longer exists."""
        data = self._check(
            self._imap.status(self._folder, "(UIDVALIDITY)"), "status"
        )
        found = _UIDVALIDITY.search(b" ".join(
            part if isinstance(part, bytes) else str(part).encode()
            for part in data
        ))
        if not found:
            raise MailboxError(f"no UIDVALIDITY in {data!r}")
        return int(found.group(1))

    def uids_since(self, uid: int | None) -> list[int]:
        """Everything newer than uid, or the whole folder when given None."""
        criterion = "ALL" if uid is None else f"UID {uid + 1}:*"
        data = self._check(self._imap.uid("SEARCH", None, criterion), "search")
        raw = data[0] or b""
        found = [int(part) for part in raw.split()]
        # "UID n:*" is inclusive of the highest UID even when nothing is newer,
        # so the server can answer with the message we already have.
        return sorted(u for u in found if uid is None or u > uid)

    def fetch(self, uid: int) -> bytes:
        data = self._check(
            self._imap.uid("FETCH", str(uid), "(RFC822)"), f"fetch {uid}"
        )
        for part in data:
            if isinstance(part, tuple) and len(part) > 1:
                return part[1]
        raise MailboxError(f"fetch {uid}: no message body in {data!r}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_imapbox.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add renewal/mail/imapbox.py tests/test_imapbox.py
git commit -m "feat(mail): a read-only IMAP connection"
```

---

### Task 4: One poll

**Files:**
- Create: `renewal/mail/poll.py`
- Test: `tests/test_mail_poll.py`

**Interfaces:**
- Consumes: `Mailbox` (Task 3), `receive(..., agency=)` (Task 2), `MailPollState` (Task 1).
- Produces: `PollResult(seen, ingested, duplicate, failed)` and
  `poll_once(session, store, *, settings, client=None, on_document=None, opener=None) -> PollResult`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mail_poll.py`:

```python
import email.utils
from renewal.mail.poll import poll_once
from renewal.models import Agency, Document, InboundMessage


def _raw(subject, message_id, body="A cancellation takes effect 2026-12-01."):
    return (
        f"From: underwriting@carrier.example\r\n"
        f"To: anne@agency.example\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Date: {email.utils.formatdate()}\r\n"
        f"\r\n{body}\r\n"
    ).encode()


class FakeBox:
    """Stands in for Mailbox. Same three methods the poll uses."""

    def __init__(self, messages, validity=99):
        self.messages = messages          # {uid: raw bytes}
        self.validity = validity
        self.fetched = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def uid_validity(self):
        return self.validity

    def uids_since(self, uid):
        return sorted(u for u in self.messages if uid is None or u > uid)

    def fetch(self, uid):
        self.fetched.append(uid)
        return self.messages[uid]


def _settings_with_imap(**overrides):
    import dataclasses
    from tests.test_dates_llm import _settings

    base = {"imap_host": "imap.example.com", "imap_user": "anne",
            "imap_password": "app-password", "imap_folder": "Carriers"}
    return dataclasses.replace(_settings(), **(base | overrides))


def _poll(session, store, box, **kwargs):
    return poll_once(session, store, settings=_settings_with_imap(),
                     opener=lambda settings: box, **kwargs)


def test_new_mail_becomes_a_message_and_a_document(session, store):
    box = FakeBox({4: _raw("Cancellation", "<a@carrier.example>")})
    result = _poll(session, store, box)

    assert result.seen == 1 and result.ingested == 1
    assert session.query(InboundMessage).count() == 1
    # The body is stored as a document too: the explanation is often in prose
    # while the attachment is a bare form.
    assert session.query(Document).count() == 1


def test_the_same_message_twice_is_ingested_once(session, store):
    box = FakeBox({4: _raw("Cancellation", "<a@carrier.example>")})
    _poll(session, store, box)
    session.query(MailPollState).delete()   # forget the mark, keep the ledger
    session.flush()

    result = _poll(session, store, box)

    assert result.duplicate == 1
    assert session.query(InboundMessage).count() == 1


def test_the_mark_advances_so_the_next_poll_asks_for_less(session, store):
    box = FakeBox({4: _raw("One", "<1@c.example>"),
                   9: _raw("Two", "<2@c.example>")})
    _poll(session, store, box)

    state = session.query(MailPollState).one()
    assert state.last_uid == 9
    assert state.uid_validity == 99

    box.fetched.clear()
    _poll(session, store, box)
    assert box.fetched == []


def test_a_rebuilt_folder_is_read_again_from_the_start(session, store):
    """A changed UIDVALIDITY means every UID we remember is a number about a
    folder that no longer exists."""
    box = FakeBox({4: _raw("One", "<1@c.example>")})
    _poll(session, store, box)

    box.validity = 100
    box.fetched.clear()
    result = _poll(session, store, box)

    assert box.fetched == [4]
    assert result.duplicate == 1      # re-read, but the ledger still holds


def test_one_unparseable_message_does_not_stop_the_poll(session, store):
    box = FakeBox({4: b"\xff\xfe not a message at all",
                   5: _raw("Real", "<real@c.example>")})
    result = _poll(session, store, box)

    assert result.ingested == 1
    assert session.query(MailPollState).one().last_uid == 5


def test_a_failed_connection_is_recorded_rather_than_raised(session, store):
    """An intake that has been broken since Thursday must not be invisible."""
    def boom(settings):
        raise OSError("connection refused")

    result = poll_once(session, store, settings=_settings_with_imap(),
                       opener=boom)

    assert result.seen == 0
    assert "connection refused" in session.query(MailPollState).one().last_error


def test_no_imap_host_polls_nothing(session, store):
    result = poll_once(session, store,
                       settings=_settings_with_imap(imap_host=""),
                       opener=lambda settings: FakeBox({}))
    assert result.seen == 0
    assert session.query(MailPollState).count() == 0
```

The agency row is seeded by a migration, so `session.query(Agency).first()` is
available without creating one.

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_mail_poll.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.mail.poll'`.

- [ ] **Step 3: Write the module**

Create `renewal/mail/poll.py`:

```python
"""One pass over the mailbox.

What is new, hand each message to the intake that already exists, remember how
far we got. The remembering is an optimisation and nothing more: correctness
lives on InboundMessage's unique (agency_id, message_id), so losing the mark
costs a re-read and never a duplicate.

The mark advances past a message that could not be parsed. Leaving it behind
would jam every later message in the folder behind one malformed one, which is
a worse failure than losing the ability to retry something that will not parse
on the second attempt either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.mail.imapbox import Mailbox
from renewal.mail.intake import receive
from renewal.mail.parse import parse_mime
from renewal.models import Agency, InboundMessage, MailPollState

logger = logging.getLogger(__name__)

AGENCY_ID = 1


@dataclass(frozen=True)
class PollResult:
    seen: int = 0
    ingested: int = 0
    duplicate: int = 0
    failed: int = 0


def _open(settings: Settings) -> Mailbox:
    return Mailbox(
        settings.imap_host, settings.imap_user, settings.imap_password,
        folder=settings.imap_folder, port=settings.imap_port,
    )


def _state(session: Session, settings: Settings) -> MailPollState:
    row = session.scalar(
        select(MailPollState)
        .where(MailPollState.host == settings.imap_host)
        .where(MailPollState.folder == settings.imap_folder)
    )
    if row is None:
        row = MailPollState(host=settings.imap_host, folder=settings.imap_folder)
        session.add(row)
        session.flush()
    return row


def poll_once(
    session: Session,
    store: BlobStore,
    *,
    settings: Settings,
    client=None,
    on_document=None,
    opener=_open,
) -> PollResult:
    """Read what is new and hand it to intake. Never raises: a mailbox that is
    down is a thing to report on a page, not a thing to crash a clock."""
    if not (settings.imap_host and settings.imap_user):
        return PollResult()

    state = _state(session, settings)
    agency = session.get(Agency, AGENCY_ID)
    seen = ingested = duplicate = failed = 0

    try:
        with opener(settings) as box:
            validity = box.uid_validity()
            since = state.last_uid
            if state.uid_validity is not None and state.uid_validity != validity:
                # The folder was rebuilt or renamed. Every UID we remember is a
                # number about a folder that no longer exists.
                logger.info(
                    "mailbox uidvalidity changed folder=%s %s -> %s",
                    settings.imap_folder, state.uid_validity, validity,
                )
                since = None
            state.uid_validity = validity

            for uid in box.uids_since(since):
                seen += 1
                try:
                    parsed = parse_mime(box.fetch(uid))
                    # Asked before receive() rather than inferred after it:
                    # receive() returns the existing row for a duplicate, and
                    # that row already says 'processed', so afterwards the two
                    # cases are indistinguishable.
                    already = session.scalar(
                        select(InboundMessage.id)
                        .where(InboundMessage.agency_id == agency.id)
                        .where(InboundMessage.message_id == parsed.message_id)
                    )
                    message = receive(
                        session, store, parsed, client=client,
                        settings=settings, on_document=on_document,
                        # The mailbox is the routing: we logged into this
                        # account, so whose mail this is was settled before the
                        # envelope was read.
                        agency=agency,
                    )
                    if already is not None:
                        duplicate += 1
                    elif message.processing_status == "processed":
                        ingested += 1
                    else:
                        failed += 1
                except Exception:  # noqa: BLE001 - one message never stops a poll
                    logger.exception(
                        "mail poll failed uid=%s folder=%s", uid,
                        settings.imap_folder,
                    )
                    failed += 1
                # Advanced whatever happened, so a message that will never
                # parse cannot jam the folder behind it.
                state.last_uid = uid if state.last_uid is None else max(
                    state.last_uid, uid
                )
        state.last_error = None
    except Exception as failure:  # noqa: BLE001 - reported, never raised
        logger.exception("mail poll failed host=%s", settings.imap_host)
        state.last_error = f"{type(failure).__name__}: {failure}"

    state.last_polled_at = datetime.now(timezone.utc)
    state.last_seen = seen
    state.last_ingested = ingested
    session.flush()
    return PollResult(seen=seen, ingested=ingested, duplicate=duplicate,
                      failed=failed)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mail_poll.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add renewal/mail/poll.py tests/test_mail_poll.py
git commit -m "feat(mail): one poll of the mailbox"
```

---

### Task 5: The clock, the settings, and the way to run it by hand

**Files:**
- Create: `renewal/mail/clock.py`, `scripts/poll_mail.py`
- Modify: `renewal/config.py`, `renewal/app.py`, `.env.example`
- Test: `tests/test_mail_poll.py`

**Interfaces:**
- Produces: `MailClock(session_factory, store, settings, *, tick_seconds, sleep, client)`
  with `tick()`, `run()`, `start()`; `Settings.imap_host`, `imap_port`,
  `imap_user`, `imap_password`, `imap_folder`, `imap_poll_seconds`.

- [ ] **Step 1: Add the settings**

In `renewal/config.py`, in the deployment block:

```python
    # Pulling mail out of a real mailbox. An empty host turns polling off,
    # which is the default, exactly as an empty SMTP_HOST turns notifications
    # off. Credentials are deployment configuration and never reach the
    # database or a page.
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = 300
```

and in `load_settings()`:

```python
        imap_host=os.environ.get("IMAP_HOST", ""),
        imap_port=int(os.environ.get("IMAP_PORT", "993")),
        imap_user=os.environ.get("IMAP_USER", ""),
        imap_password=os.environ.get("IMAP_PASSWORD", ""),
        imap_folder=os.environ.get("IMAP_FOLDER", "INBOX"),
        imap_poll_seconds=int(os.environ.get("IMAP_POLL_SECONDS", "300")),
```

- [ ] **Step 2: Write the clock**

Create `renewal/mail/clock.py`, modelled exactly on `renewal/digest/clock.py`
— same injected `sleep`, same daemon thread, same "a tick that raises is
logged and the loop continues":

```python
"""The clock that asks the mailbox whether anything arrived.

The same shape as the digest clock beside it, and the same one failure mode
stated the same way: work in this process dies with this process. A poll that
does not happen costs nothing, because the next one asks the same question and
the mailbox still holds the same mail.
"""

from __future__ import annotations

import logging
import threading
import time

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.mail.poll import poll_once

logger = logging.getLogger(__name__)


class MailClock:
    def __init__(
        self, session_factory, store: BlobStore, settings: Settings, *,
        tick_seconds: int = 300, sleep=time.sleep, client=None, runner=None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._settings = settings
        self._tick_seconds = tick_seconds
        self._sleep = sleep
        self._client = client
        self._runner = runner

    def tick(self):
        with self._session_factory() as session:
            on_document = None
            if self._runner is not None:
                on_document = lambda document_id: self._runner.submit(
                    _process, self._session_factory, self._store, document_id,
                    self._client, self._settings,
                )
            result = poll_once(
                session, self._store, settings=self._settings,
                client=self._client, on_document=on_document,
            )
            session.commit()
            return result

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a clock outlives one bad night
                logger.exception("mail poll tick failed")
            self._sleep(self._tick_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="mailpoll", daemon=True)
        thread.start()
        return thread
```

`_process` is a module-level function wrapping
`renewal.background.process_document` with the arguments it needs, so the
lambda closes over nothing that a thread could see change.

- [ ] **Step 3: Wire it into production**

In `renewal/app.py`, after the digest clock:

```python
if _settings.imap_host and _settings.imap_poll_seconds > 0:
    MailClock(
        _session_factory, _store, _settings,
        tick_seconds=_settings.imap_poll_seconds,
        client=_model_client, runner=_runner,
    ).start()
```

This needs `create_app`'s store, model client and runner to be named locals
rather than built inline in the call; hoist them.

- [ ] **Step 4: Add the command-line poll**

Create `scripts/poll_mail.py`, mirroring `scripts/digest.py`: builds settings,
a session factory and a store, runs one `MailClock(...).tick()`, prints what it
found, exits.

- [ ] **Step 5: Document the variables**

In `.env.example`, after the notification block:

```
# Pulling mail out of a mailbox. Empty IMAP_HOST turns it off, which is the
# default. IMAP_FOLDER is the folder or label carrier mail is filtered into —
# everything in it is ingested, so point it at a folder you control rather
# than at INBOX. IMAP_PASSWORD must be an app password: Gmail and Outlook both
# refuse an account password over IMAP once two-factor is on.
IMAP_HOST=
IMAP_PORT=993
IMAP_USER=
IMAP_PASSWORD=
IMAP_FOLDER=INBOX
IMAP_POLL_SECONDS=300
```

- [ ] **Step 6: Test the clock**

Append to `tests/test_mail_poll.py` a test that `tick()` commits and one that
`run()` survives a raising tick, both modelled on `tests/test_digest_clock.py`
including its `_StoppingClock` subclass, so the daemon test does not leak an
unhandled thread exception.

- [ ] **Step 7: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add renewal/mail/clock.py renewal/config.py renewal/app.py scripts/poll_mail.py .env.example tests/test_mail_poll.py
git commit -m "feat(mail): a clock that pulls the mailbox"
```

---

### Task 6: It says when it is broken

Polling is invisible when it works, which is right. When it is not working it
must not be invisible.

**Files:**
- Modify: `renewal/web/settings.py`, `renewal/templates/settings.html`, `README.md`
- Test: `tests/test_web_settings.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_settings_page_reports_the_last_mail_poll(client_app, db):
    from datetime import datetime, timezone

    from renewal.models import MailPollState

    db.add(MailPollState(
        host="imap.example.com", folder="Carriers", last_seen=3,
        last_ingested=2, last_polled_at=datetime.now(timezone.utc),
        last_error="OSError: connection refused",
    ))
    db.commit()

    page = client_app.get("/settings").text
    assert "Carriers" in page
    assert "connection refused" in page
```

- [ ] **Step 2: Add it to the route and the template**

Read the newest `MailPollState` in the settings route, pass it as
`mail_poll`, and render a panel in `settings.html` in the shape the other
panels use: the folder, when it last ran, what it found, and — only when
`last_error` is set — the error, marked with the `.notice.bad` treatment.
When no row exists, say polling has never run, and when `IMAP_HOST` is unset
say it is turned off and where that lives, mirroring the existing
`mail_configured` notice for SMTP.

- [ ] **Step 3: Document it in the README**

Add a "Pulling mail from a mailbox" section after "The daily summary": what it
does, that it never writes to the mailbox, that `IMAP_FOLDER` should be a
folder you filter into rather than `INBOX`, how to get an app password, and the
cron alternative with `IMAP_POLL_SECONDS=0`.

- [ ] **Step 4: Run the suite and commit**

```bash
.venv/bin/pytest -q
git add renewal/web/settings.py renewal/templates/settings.html README.md tests/test_web_settings.py
git commit -m "feat(mail): the settings page says when the mailbox stopped answering"
```

---

## Verification

- [ ] `.venv/bin/pytest -q` — green.
- [ ] `alembic upgrade head`, `downgrade -1`, `upgrade head` — reversible.
- [ ] With `IMAP_HOST` unset, `python -m scripts.poll_mail` reports nothing to do and writes no row.
- [ ] Against a real mailbox: a message with a PDF appears in the inbox; polling twice ingests it once; the message is still unread in the mail client afterwards.
