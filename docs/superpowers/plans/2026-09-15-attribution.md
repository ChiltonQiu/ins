# Attribution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record which account made each human judgment about the record — corrected a field, confirmed a date, cleared an item, filed a document, built a comparison — and show it where the judgment shows.

**Architecture:** One nullable `user_id` column on each of the eight decision tables, beside the `actor` column that already says human-or-machine. The gate already resolves the signed-in user on every request; it gains the id, routes read it off `request.state`, and every service function takes `user_id: int | None = None` the way it already takes `actor`. Nothing below `renewal/web/` learns what a request is.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, Jinja2, PostgreSQL, pytest + `fastapi.testclient.TestClient`.

**Spec:** `docs/superpowers/specs/2026-09-15-attribution-design.md`

## Global Constraints

- **`user_id` is always nullable, everywhere, permanently.** Historical rows have no user and none may be invented for them. No task in this plan backfills.
- **`actor` keeps its meaning.** It says human-or-machine. `user_id` says which human. Neither replaces the other, and no task removes `actor`.
- **The library never imports a request.** Service functions take `user_id: int | None = None`. Only modules under `renewal/web/` may touch `request.state`.
- **Foreign keys are `ON DELETE RESTRICT`.** A user who decided something cannot be deleted; `is_active` is how an account is turned off. `UserSession`'s CASCADE is not the precedent here — a session is worthless without its user, a correction is not.
- **A NULL user renders as nothing.** No "unknown", no "system". A blank is honest about the three different things a NULL means; a label is a guess.
- **Display uses `display_name`, never `email`.** The address is a credential and a contact detail, not a byline.
- Run the suite with `.venv/bin/pytest`. It needs PostgreSQL; override the database with `TEST_DATABASE_URL`.
- Baseline before this plan starts: **850 tests passing, 3 deselected.**

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `migrations/versions/*_attribution.py` | The eight `user_id` columns. |
| `tests/test_attribution.py` | The columns, the FK behaviour, and the service-level plumbing. |
| `tests/test_web_attribution.py` | End to end: a signed-in request writes a row pointing at that account. |

**Modified:**

| File | Change |
|---|---|
| `renewal/models.py` | `user_id` on `Correction`, `DateEvent`, `ManualDate`, `ManualDateEvent`, `AttentionEvent`, `DocumentLink`, `Comparison`, `Reclassification`. |
| `renewal/web/security.py` | Put the user's id on `request.state` beside their email. |
| `renewal/web/deps.py` | `acting_user_id(request)`. |
| `renewal/corrections.py` | `record_correction(..., user_id=None)`. |
| `renewal/web/corrections.py` | Three routes take a `Request` and pass it. |
| `renewal/dates/service.py` | `confirm`/`dismiss` take `user_id`. |
| `renewal/web/calendar.py` | Four routes pass it. |
| `renewal/attention/rules.py` | `resolve` takes `user_id`. |
| `renewal/web/attention.py` | Two routes pass it. |
| `renewal/resolve/service.py` | `assign` takes `user_id`. |
| `renewal/web/unmatched.py` | Two routes pass it. |
| `renewal/web/inbox.py` | `set_policy` passes it; the detail view gains its corrections list. |
| `renewal/comparison.py` | `build_matrix`, `build_comparison` and `reclassify` take `user_id`. |
| `renewal/web/comparison.py` | Three routes pass it. |
| `renewal/calendarview/agenda.py` | `AgendaEntry.decided_by`. |
| `renewal/templates/calendar.html` | Who confirmed or dismissed, on the row. |
| `renewal/templates/inbox_detail.html` | The corrections list. |
| `README.md` | Replace the sentence saying this is not recorded. |

---

### Task 1: The gate hands over an id

`renewal/web/security.py:88` already looks the user up on every request and
reads two fields off the ORM object before the session closes. It gains a
third. This is the only place in the application that knows who is asking, and
every later task depends on it.

**Files:**
- Modify: `renewal/web/security.py:80-108`
- Modify: `renewal/web/deps.py`
- Test: `tests/test_web_attribution.py`

**Interfaces:**
- Produces: `request.state.user_id: int` on every authenticated request, and
  `renewal.web.deps.acting_user_id(request) -> int | None`.

- [x] **Step 1: Write the failing test**

Create `tests/test_web_attribution.py`:

```python
"""Who did it, end to end.

A signed-in request writes a row that points at that account. The gate is the
only place that knows who is asking, so every test here goes through it rather
than calling a service function with a user_id it made up.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import User
from renewal.web import create_app
from tests.authhelp import EMAIL, sign_in


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
        session_cookie_secure=False,
    )


@pytest.fixture
def app(engine, clean_db, settings):
    from renewal.background import InlineRunner

    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
        runner=InlineRunner(),
    )


@pytest.fixture
def signed(app, engine):
    client = TestClient(app)
    sign_in(client, engine)
    return client


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _me(db) -> User:
    return db.query(User).filter_by(email=EMAIL).one()


def test_the_gate_puts_the_user_id_on_the_request(app, engine, db):
    """Every later task reads it from here. Asserted through a route rather
    than by calling the middleware, because the state is what routes see."""
    from fastapi import Request

    seen = {}

    @app.get("/_whoami_test")
    def whoami(request: Request):
        from renewal.web.deps import acting_user_id

        seen["user_id"] = acting_user_id(request)
        return {"ok": True}

    client = TestClient(app)
    sign_in(client, engine)
    client.get("/_whoami_test")

    assert seen["user_id"] == _me(db).id


def test_an_unauthenticated_path_has_no_user(app):
    """acting_user_id reads through getattr with a default: the .ics feed and
    the mail webhook never pass the gate and have no user on their state."""
    from renewal.web.deps import acting_user_id

    class _Bare:
        state = type("S", (), {})()

    assert acting_user_id(_Bare()) is None
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_web_attribution.py -q`
Expected: FAIL — `ImportError: cannot import name 'acting_user_id'`.

- [x] **Step 3: Carry the id through the gate**

In `renewal/web/security.py`, change the identity tuple to carry three fields.
Replace the comment and the declaration:

```python
        # Read out as plain strings rather than kept as an ORM object: the
        # session closes here, and a template rendering a detached instance
        # later would raise. The topbar needs an address and a name; the id is
        # what every attributed write points at.
        identity: tuple[int, str, str] | None = None
        if token:
            with session_factory() as session:
                user = lookup_session(
                    session, token,
                    ttl_hours=effective(
                        session, deps.settings
                    ).session_ttl_hours,
                )
                if user is not None:
                    identity = (user.id, user.email, user.display_name)
```

and the assignment at the end:

```python
        (
            request.state.user_id,
            request.state.user_email,
            request.state.user_display_name,
        ) = identity
```

- [x] **Step 4: Add the accessor**

In `renewal/web/deps.py`, below the `Deps` dataclass:

```python
def acting_user_id(request) -> int | None:
    """Who is making this request, for a row that records a decision.

    getattr with a default rather than a bare attribute read: the .ics feed and
    the mail webhook never pass the gate and have no user on their state.
    Neither writes an attributed row today, and this is what keeps it safe if
    one ever does.
    """
    return getattr(request.state, "user_id", None)
```

- [x] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_web_attribution.py tests/test_web_auth.py -q`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add renewal/web/security.py renewal/web/deps.py tests/test_web_attribution.py
git commit -m "feat(attribution): the gate hands over a user id"
```

---

### Task 2: The columns

Eight nullable columns and one migration. Nothing writes them yet — that is
Tasks 3 to 6 — so this task is finished when the schema accepts an attributed
row and refuses to lose one.

**Files:**
- Modify: `renewal/models.py`
- Create: `migrations/versions/<rev>_attribution.py`
- Test: `tests/test_attribution.py`

**Interfaces:**
- Produces: `user_id: Mapped[int | None]` on `Correction`, `DateEvent`,
  `ManualDate`, `ManualDateEvent`, `AttentionEvent`, `DocumentLink`,
  `Comparison`, `Reclassification`.

- [x] **Step 1: Write the failing test**

Create `tests/test_attribution.py`:

```python
"""Which account made this judgment.

Nullable everywhere and forever: every row written before this change has no
user, and a machine-written row has none by definition. The actor column keeps
saying human-or-machine; user_id says which human.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from renewal.auth.passwords import hash_password
from renewal.ingest import ingest_pdf
from renewal.models import AttentionEvent, AttentionItem, User
from tests.pdfmaker import make_text_pdf

ATTRIBUTED = (
    "correction", "date_event", "manual_date", "manual_date_event",
    "attention_event", "document_link", "comparison", "reclassification",
)


@pytest.fixture
def user(session):
    person = User(email="anne@agency.com", password_hash=hash_password("x"),
                  display_name="Anne Ramirez")
    session.add(person)
    session.flush()
    return person


@pytest.fixture
def item(session, store):
    document = ingest_pdf(
        session, store, data=make_text_pdf([["x"]]),
        original_filename="a.pdf", source="manual_upload", agency_id=1,
    )
    row = AttentionItem(document_id=document.id,
                        reason_code="unmatched_document",
                        reason_text="Could not be attached to a client")
    session.add(row)
    session.flush()
    return row


def test_every_decision_table_has_the_column(session):
    inspector = inspect(session.get_bind())
    for table in ATTRIBUTED:
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        assert "user_id" in columns, f"{table} is not attributed"
        assert columns["user_id"]["nullable"], f"{table}.user_id must be nullable"


def test_a_decision_can_name_its_user(session, user, item):
    event = AttentionEvent(attention_item_id=item.id, action="done",
                           actor="human", user_id=user.id)
    session.add(event)
    session.flush()
    assert session.get(AttentionEvent, event.id).user_id == user.id


def test_a_decision_may_name_nobody(session, item):
    """Three different things mean NULL: written before this change, written
    by the machine, or written by an unauthenticated path. The column does not
    try to tell them apart and neither may anything reading it."""
    event = AttentionEvent(attention_item_id=item.id, action="done",
                           actor="auto")
    session.add(event)
    session.flush()
    assert event.user_id is None


def test_a_user_who_decided_something_cannot_be_deleted(session, user, item):
    """is_active is how an account is turned off. Deleting one would take the
    record of what they decided with it."""
    session.add(AttentionEvent(attention_item_id=item.id, action="done",
                               actor="human", user_id=user.id))
    session.flush()

    with pytest.raises(IntegrityError):
        session.delete(user)
        session.flush()
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_attribution.py -q`
Expected: FAIL — `assert "user_id" in columns` for `correction`.

- [x] **Step 3: Add the columns to the models**

In `renewal/models.py`, add this helper below `_created_at()`:

```python
def _decided_by() -> Mapped[int | None]:
    """Which account made this judgment.

    Nullable permanently: rows written before attribution existed have no user,
    and a row the pipeline wrote has none by definition. RESTRICT rather than
    CASCADE — a user who decided something cannot be deleted, because deleting
    them would take the record of the decision with them. is_active is how an
    account is turned off.
    """
    return mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
```

Then add `user_id: Mapped[int | None] = _decided_by()` to each of the eight
classes: `Correction` (after `note`), `DateEvent` (after `actor`), `ManualDate`
(after `created_by`), `ManualDateEvent` (after `actor`), `AttentionEvent`
(after `actor`), `DocumentLink` (after `candidates`), `Comparison` (at the end
of its column list), `Reclassification` (after `note`).

`ForeignKey` is already imported at `renewal/models.py:16-29`.

- [x] **Step 4: Write the migration**

```bash
.venv/bin/alembic revision -m "attribution"
```

Fill the generated file:

```python
TABLES = (
    "correction", "date_event", "manual_date", "manual_date_event",
    "attention_event", "document_link", "comparison", "reclassification",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("user_id", sa.Integer(), nullable=True))
        # RESTRICT: deleting a user would take the record of what they decided
        # with them. is_active is how an account is turned off.
        op.create_foreign_key(
            f"fk_{table}_user_id", table, "app_user", ["user_id"], ["id"],
            ondelete="RESTRICT",
        )
    # Deliberately no backfill. Every existing row was written before anyone
    # could be named, and attributing the history to the only account that
    # happens to exist would manufacture evidence.


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_constraint(f"fk_{table}_user_id", table, type_="foreignkey")
        op.drop_column(table, "user_id")
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_attribution.py -q`
Expected: PASS (4 tests).

- [x] **Step 6: Commit**

```bash
git add renewal/models.py migrations/versions tests/test_attribution.py
git commit -m "feat(attribution): a user_id on every decision table"
```

---

### Task 3: Corrections carry it

The training-data one, and the reason this change matters most. A corrections
table that cannot distinguish a senior producer's correction from a temp's
typo is not training data.

**Files:**
- Modify: `renewal/corrections.py:20-45`
- Modify: `renewal/web/corrections.py`
- Test: `tests/test_web_attribution.py`, `tests/test_corrections.py`

**Interfaces:**
- Consumes: `acting_user_id` from Task 1, `Correction.user_id` from Task 2.
- Produces: `record_correction(session, *, ..., user_id: int | None = None)`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_web_attribution.py`:

```python
def _extracted_field(db):
    from renewal.models import Document, ExtractedField, Extraction

    document = Document(blob_sha256="a" * 64, original_filename="a.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        agency_id=1)
    db.add(document)
    db.flush()
    extraction = Extraction(document_id=document.id, extractor_version="v1",
                            model_id="m", status="ok")
    db.add(extraction)
    db.flush()
    field = ExtractedField(extraction_id=extraction.id,
                           field_path="policy.number", value="P-1",
                           confidence=0.9)
    db.add(field)
    db.commit()
    return field


def test_a_correction_records_who_made_it(signed, db):
    from renewal.models import Correction

    field = _extracted_field(db)
    response = signed.post(f"/fields/{field.id}/correct",
                           data={"corrected_value": "P-2"})
    assert response.status_code == 204

    correction = db.query(Correction).one()
    assert correction.user_id == _me(db).id
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_web_attribution.py -q`
Expected: FAIL — `assert None == 1`.

- [x] **Step 3: Thread it through the service**

In `renewal/corrections.py`, add the parameter to `record_correction`'s
signature after `note`:

```python
    note: str | None = None,
    user_id: int | None = None,
```

and the field to the constructed row after `note=note`:

```python
        note=note,
        user_id=user_id,
```

- [x] **Step 4: Pass it from the three routes**

In `renewal/web/corrections.py`, import what is needed:

```python
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response

from renewal.corrections import record_correction
from renewal.models import ExtractedField
from renewal.web.deps import Deps, acting_user_id
```

Give each of the three routes a `request: Request` first parameter and pass
`user_id=acting_user_id(request)` into `record_correction`. For
`correct_field`:

```python
    @router.post("/fields/{field_id}/correct", status_code=204)
    def correct_field(
        request: Request, field_id: int, corrected_value: str = Form(...)
    ):
        with session_factory() as session:
            field = session.get(ExtractedField, field_id)
            if field is None:
                raise HTTPException(status_code=404, detail="no such field")
            record_correction(
                session,
                extraction_id=field.extraction_id,
                extracted_field_id=field.id,
                field_path=field.field_path,
                kind="wrong_value",
                extracted_value=field.value,
                corrected_value=corrected_value,
                user_id=acting_user_id(request),
            )
            session.commit()
        return Response(status_code=204)
```

For `reject_field`, the same `request: Request` first parameter and the same
`user_id=acting_user_id(request)` added to its `record_correction` call. For
the third route on `/extractions/{extraction_id}/fields`, read the file and add
the same parameter and keyword to every `record_correction` call inside it.

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_attribution.py tests/test_corrections.py tests/test_web_corrections.py -q`
Expected: PASS. (If `tests/test_web_corrections.py` does not exist, run the
first two.)

- [x] **Step 6: Commit**

```bash
git add renewal/corrections.py renewal/web/corrections.py tests/test_web_attribution.py
git commit -m "feat(attribution): corrections record who made them"
```

---

### Task 4: Dates carry it

Confirming a date is a claim that a deadline is real; dismissing one is a claim
that it is not. Both are decisions somebody should be able to be asked about.

**Files:**
- Modify: `renewal/dates/service.py:144-158`
- Modify: `renewal/web/calendar.py`
- Test: `tests/test_web_attribution.py`, `tests/test_dates_service.py`

**Interfaces:**
- Produces: `confirm(session, id, *, actor="human", user_id=None)`,
  `dismiss(session, id, *, actor="human", user_id=None)`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_web_attribution.py`:

```python
def _document_date(db):
    from datetime import date

    from renewal.models import Document, DocumentDate

    document = Document(blob_sha256="b" * 64, original_filename="b.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        agency_id=1)
    db.add(document)
    db.flush()
    row = DocumentDate(
        document_id=document.id, date_value=date(2026, 12, 1),
        date_type="policy_expiration", source_page=1, source_text="Expires",
        confidence=0.9, extractor_version="dates-regex-v1", pass_name="regex",
    )
    db.add(row)
    db.commit()
    return row


def test_confirming_a_date_records_who_confirmed_it(signed, db):
    from renewal.models import DateEvent

    row = _document_date(db)
    assert signed.post(f"/dates/{row.id}/confirm").status_code in (200, 303)

    event = db.query(DateEvent).one()
    assert event.action == "confirmed"
    assert event.user_id == _me(db).id


def test_adding_a_date_by_hand_records_who_added_it(signed, db):
    from renewal.models import ManualDate

    response = signed.post("/manual-dates", data={
        "title": "Call the carrier", "date_value": "2026-12-01",
        "date_type": "other",
    })
    assert response.status_code in (200, 303)

    added = db.query(ManualDate).one()
    assert added.user_id == _me(db).id
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_web_attribution.py -q`
Expected: FAIL — `assert None == 1`.

- [x] **Step 3: Thread it through the service**

In `renewal/dates/service.py`:

```python
def confirm(session: Session, document_date_id: int, *, actor: str = "human",
            user_id: int | None = None):
    event = DateEvent(document_date_id=document_date_id, action="confirmed",
                      actor=actor, user_id=user_id)
    session.add(event)
    session.flush()
    return event


def dismiss(session: Session, document_date_id: int, *, actor: str = "human",
            user_id: int | None = None):
    event = DateEvent(document_date_id=document_date_id, action="dismissed",
                      actor=actor, user_id=user_id)
    session.add(event)
    session.flush()
    return event
```

- [x] **Step 4: Pass it from the four routes**

In `renewal/web/calendar.py`, add `acting_user_id` to the deps import:

```python
from renewal.web.deps import Deps, acting_user_id
```

`confirm_date`, `dismiss_date`, `add_manual_date` and `dismiss_manual_date` all
already take `request: Request`. Pass the id in each:

```python
    @router.post("/dates/{document_date_id}/confirm")
    def confirm_date(request: Request, document_date_id: int):
        with session_factory() as session:
            confirm(session, document_date_id,
                    user_id=acting_user_id(request))
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)

    @router.post("/dates/{document_date_id}/dismiss")
    def dismiss_date(request: Request, document_date_id: int):
        with session_factory() as session:
            dismiss(session, document_date_id,
                    user_id=acting_user_id(request))
            session.commit()
        return RedirectResponse(_back_to(request), status_code=303)
```

In `add_manual_date`, add `user_id=acting_user_id(request)` to the `ManualDate(...)`
constructor beside `created_by="human"`. In `dismiss_manual_date`, add it to
the `ManualDateEvent(...)` constructor beside `actor="human"`.

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_attribution.py tests/test_dates_service.py tests/test_web_calendar.py -q`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add renewal/dates/service.py renewal/web/calendar.py tests/test_web_attribution.py
git commit -m "feat(attribution): dates record who judged them"
```

---

### Task 5: Clearing and filing carry it

An attention item cleared is a claim that it was handled. A document filed by
hand is a claim about which client it belongs to — the one place D8 lets a
human override the exact-match rule, and therefore the one most worth being
able to ask about.

**Files:**
- Modify: `renewal/attention/rules.py:208-215`
- Modify: `renewal/web/attention.py`
- Modify: `renewal/resolve/service.py:87-97`
- Modify: `renewal/web/unmatched.py`, `renewal/web/inbox.py`
- Test: `tests/test_web_attribution.py`

**Interfaces:**
- Produces: `resolve(session, item_id, *, action, actor="human", user_id=None)`,
  `assign(session, document_id, *, client_id, policy_id, candidates, user_id=None)`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_web_attribution.py`:

```python
def test_clearing_an_attention_item_records_who_cleared_it(signed, db):
    from renewal.models import AttentionEvent, AttentionItem, Document

    document = Document(blob_sha256="c" * 64, original_filename="c.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        agency_id=1)
    db.add(document)
    db.flush()
    item = AttentionItem(document_id=document.id,
                         reason_code="unmatched_document",
                         reason_text="Could not be attached to a client")
    db.add(item)
    db.commit()

    assert signed.post(f"/attention/{item.id}/done").status_code in (200, 303)

    event = db.query(AttentionEvent).one()
    assert event.action == "done"
    assert event.user_id == _me(db).id


def test_filing_a_document_by_hand_records_who_filed_it(signed, db):
    from renewal.models import Client, Document, DocumentLink

    document = Document(blob_sha256="d" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        agency_id=1)
    client = Client(display_name="Ramirez Landscaping")
    db.add_all([document, client])
    db.commit()

    response = signed.post(f"/unmatched/{document.id}/assign",
                           data={"client_id": str(client.id)})
    assert response.status_code in (200, 303)

    link = db.query(DocumentLink).one()
    assert link.method == "manual"
    assert link.user_id == _me(db).id
```

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_web_attribution.py -q`
Expected: FAIL — `assert None == 1`.

- [x] **Step 3: Thread it through the two services**

In `renewal/attention/rules.py`:

```python
def resolve(
    session: Session, item_id: int, *, action: str, actor: str = "human",
    user_id: int | None = None,
) -> AttentionEvent:
    event = AttentionEvent(attention_item_id=item_id, action=action,
                           actor=actor, user_id=user_id)
    session.add(event)
    session.flush()
    return event
```

In `renewal/resolve/service.py`:

```python
def assign(
    session: Session, document_id: int, *, client_id: int,
    policy_id: int | None, candidates: list[dict],
    user_id: int | None = None,
) -> DocumentLink:
    link = DocumentLink(
        document_id=document_id, client_id=client_id, policy_id=policy_id,
        method="manual", confidence=1.0, candidates=candidates,
        user_id=user_id,
    )
    session.add(link)
    session.flush()
    return link
```

The auto-link path at `renewal/resolve/service.py:73` is left alone: it writes
`method='auto'` and there is no person to name.

- [x] **Step 4: Pass it from the routes**

In `renewal/web/attention.py`, import `acting_user_id` alongside `Deps` and add
`request: Request` to `mark_done` and `mark_dismissed`, passing
`user_id=acting_user_id(request)` into each `resolve(...)` call.

In `renewal/web/unmatched.py` and `renewal/web/inbox.py`, add `request: Request`
to any route that is missing it and pass `user_id=acting_user_id(request)` into
every `assign(...)` call. Read each file first: `renewal/web/inbox.py`'s
`set_policy` and `renewal/web/unmatched.py`'s two routes are the call sites.

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_attribution.py tests/test_web_attention.py tests/test_web_inbox.py -q`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add renewal/attention/rules.py renewal/resolve/service.py renewal/web/attention.py renewal/web/unmatched.py renewal/web/inbox.py tests/test_web_attribution.py
git commit -m "feat(attribution): clearing and filing record who did them"
```

---

### Task 6: Comparisons carry it

`2026-09-12-comparison-matrix-design.md:899` named these two by name: who built
a comparison, and who reclassified a row.

**Files:**
- Modify: `renewal/comparison.py:175-200`, `:260-280`, `:465-480`
- Modify: `renewal/web/comparison.py`
- Test: `tests/test_web_attribution.py`

**Interfaces:**
- Produces: `build_matrix(..., user_id=None)`, `build_comparison(..., user_id=None)`,
  `reclassify(session, difference, to_materiality, note=None, user_id=None)`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_web_attribution.py`:

```python
def test_a_comparison_records_who_built_it(signed, db):
    """The picker posts two term ids; the row it writes names the person who
    pressed the button."""
    from renewal.models import Client, Comparison, Policy, PolicyTerm

    client = Client(display_name="Ramirez Landscaping")
    db.add(client)
    db.flush()
    policy = Policy(client_id=client.id, carrier_name="Acme",
                    policy_number="P-1")
    db.add(policy)
    db.flush()
    terms = [
        PolicyTerm(policy_id=policy.id, kind="bound"),
        PolicyTerm(policy_id=policy.id, kind="bound"),
    ]
    db.add_all(terms)
    db.commit()

    response = signed.post("/comparisons", data={
        "baseline": str(terms[0].id), "comparand": str(terms[1].id),
    }, follow_redirects=False)
    assert response.status_code in (200, 303)

    built = db.query(Comparison).one()
    assert built.user_id == _me(db).id
```

If `PolicyTerm` requires more columns than `policy_id` and `kind`, read
`renewal/models.py` around `class PolicyTerm` and supply them; if `POST
/comparisons` takes different form field names, read
`renewal/web/comparison.py` and use its names.

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_web_attribution.py -q`
Expected: FAIL — `assert None == 1`.

- [x] **Step 3: Thread it through the three functions**

In `renewal/comparison.py`, add `user_id: int | None = None` to the keyword
arguments of `build_matrix` and `build_comparison`, set `user_id=user_id` on
the `Comparison(...)` row `build_matrix` constructs, and have
`build_comparison` pass its `user_id` through to `build_matrix`. Then:

```python
def reclassify(
    session: Session,
    difference: Difference,
    to_materiality: str,
    note: str | None = None,
    user_id: int | None = None,
) -> Reclassification:
    log = Reclassification(
        difference_id=difference.id,
        from_materiality=difference.materiality,
        to_materiality=to_materiality,
        rule_id=difference.rule_id,
        note=note,
        user_id=user_id,
    )
```

The pipeline's automatic call in `renewal/pipeline.py` passes no `user_id` and
must not: nobody built that comparison.

- [x] **Step 4: Pass it from the routes**

In `renewal/web/comparison.py`, import `acting_user_id`, add `request: Request`
to any of the three routes missing it, and pass
`user_id=acting_user_id(request)` into every `build_matrix`, `build_comparison`
and `reclassify` call.

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_attribution.py tests/test_web_comparison.py tests/test_comparison.py tests/test_auto_renewal.py -q`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add renewal/comparison.py renewal/web/comparison.py tests/test_web_attribution.py
git commit -m "feat(attribution): comparisons record who built them"
```

---

### Task 7: It shows

A stored attribution nobody can read is a migration, not a feature. Two
screens: the calendar row says who judged a date, and the document detail gains
a corrections list — the first place in the application the corrections table
is visible to the person filling it.

**Files:**
- Modify: `renewal/calendarview/agenda.py`
- Modify: `renewal/templates/calendar.html`
- Modify: `renewal/web/inbox.py`, `renewal/templates/inbox_detail.html`
- Modify: `README.md:110`
- Test: `tests/test_agenda.py`, `tests/test_web_attribution.py`

**Interfaces:**
- Consumes: every column from Tasks 2 to 6.
- Produces: `AgendaEntry.decided_by: str | None`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_web_attribution.py`:

```python
def test_the_calendar_says_who_confirmed_a_date(signed, db):
    row = _document_date(db)
    signed.post(f"/dates/{row.id}/confirm")

    page = signed.get("/calendar").text
    assert "Test" in page  # authhelp signs in as display_name "Test"


def test_the_detail_view_lists_corrections_with_their_author(signed, db):
    field = _extracted_field(db)
    signed.post(f"/fields/{field.id}/correct", data={"corrected_value": "P-2"})

    document_id = db.query(type(field)).get(field.id).extraction.document_id
    page = signed.get(f"/documents/{document_id}/review").text
    assert "P-2" in page
    assert "Test" in page


def test_a_row_with_no_user_renders_nothing_rather_than_a_guess(signed, db):
    """Three different things mean NULL and the page must not claim to tell
    them apart."""
    from renewal.dates.service import confirm

    row = _document_date(db)
    confirm(db, row.id, actor="auto")
    db.commit()

    page = signed.get("/calendar").text
    assert "unknown" not in page.lower()
    assert "system" not in page.lower()
```

Check the detail route's real URL in `renewal/web/inbox.py` before running —
the plan's other tasks use `/documents/{id}/review`; use whatever that file
registers.

- [x] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/pytest tests/test_web_attribution.py -q`
Expected: FAIL — the page does not contain "Test".

- [x] **Step 3: Carry the name onto the agenda entry**

In `renewal/calendarview/agenda.py`, add `decided_by: str | None` to
`AgendaEntry`, join `app_user` through the latest-event subquery's `user_id`,
and select `User.display_name` into it. The existing
`_latest_event_subquery(DateEvent, "document_date_id")` already provides the
event row; extend its selected columns to include `user_id`, outerjoin
`User` on it, and pass the display name into every `AgendaEntry(...)`
constructed in that module — including the manual-date branch, which joins
`ManualDateEvent` the same way.

- [x] **Step 4: Show it on the calendar row**

In `renewal/templates/calendar.html`, beside the existing status flag at line
91:

```html
  <span class="flag status-flag">{{ entry.status }}</span>
  {% if entry.decided_by %}
  <span class="meta by">by {{ entry.decided_by }}</span>
  {% endif %}
```

The `{% if %}` is the rule from the spec: a NULL renders as nothing at all.

- [x] **Step 5: List corrections on the detail view**

In `renewal/web/inbox.py`'s detail route, after `rate = verification_rate(fields)`:

```python
                corrections = (
                    session.query(Correction, User.display_name)
                    .outerjoin(User, User.id == Correction.user_id)
                    .filter(Correction.extraction_id == extraction.id)
                    .order_by(Correction.id.desc())
                    .all()
                )
```

Add `"corrections": corrections` to the template context, and import
`Correction` and `User` from `renewal.models`.

In `renewal/templates/inbox_detail.html`, add a section:

```html
{% if corrections %}
<section class="panel">
  <h2>Corrections</h2>
  <ul class="corrections">
    {% for correction, who in corrections %}
    <li>
      <code>{{ correction.field_path }}</code>
      {% if correction.corrected_value %}→ {{ correction.corrected_value }}
      {% else %}removed{% endif %}
      {% if who %}<span class="meta by">{{ who }}</span>{% endif %}
    </li>
    {% endfor %}
  </ul>
</section>
{% endif %}
```

- [x] **Step 6: Say so in the README**

Replace `README.md:110-111`:

```markdown
There are no roles: every account can do everything. What each account *did* is
recorded: a correction, a confirmed or dismissed date, a cleared attention
item, a document filed by hand, a comparison built, a materiality overridden —
each row names the account that made it. Rows written before that change, and
rows written by the pipeline rather than a person, name nobody and are shown
blank rather than guessed at.
```

- [x] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, 850 baseline plus the new tests.

- [x] **Step 8: Commit**

```bash
git add renewal/calendarview/agenda.py renewal/templates renewal/web/inbox.py README.md tests/test_web_attribution.py
git commit -m "feat(attribution): it shows"
```

---

## Verification

- [x] `.venv/bin/pytest -q` — green.
- [x] `.venv/bin/alembic upgrade head`, then `downgrade -1`, then `upgrade head` — reversible.
- [x] Sign in, confirm a date on `/calendar`, and see your own name on the row.
- [x] `SELECT count(*) FROM correction WHERE user_id IS NULL` on the dev database returns the pre-change count, unchanged — nothing was backfilled.
