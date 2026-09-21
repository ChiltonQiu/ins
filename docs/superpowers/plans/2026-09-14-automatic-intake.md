# Automatic Intake and the Inbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the renewal-run flow, run document intake in the background, and put every document that arrives on one screen with its state and its fix — so a renewal that arrives by email becomes a compared, drafted, flagged renewal with no human action at all.

**Architecture:** Intake keeps the pipeline it already has. `ingest_document` is split so its stages can be re-run on an existing document, a small in-process runner moves those stages off the request, and `Document.status` records only the one thing that cannot be derived — that work is in flight. Everything else the inbox shows is computed at read time from rows the pipeline already writes. The pipeline gains one new stage: after promoting a term, it builds the renewal comparison and its draft, which is what closes the circularity where `premium_change` was raised inside the very click it existed to prompt.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, Jinja2, PostgreSQL, pytest + `fastapi.testclient.TestClient`, `concurrent.futures.ThreadPoolExecutor`, `smtplib`.

**Spec:** `docs/superpowers/specs/2026-09-14-automatic-intake-design.md`

## Global Constraints

- **Every gate stays exactly as strict.** D8's exact-policy-number rule is unchanged. The promote gate in `run_promote_stage` is unchanged. No task in this plan loosens either. Automation means the clean case needs no touches, not that the ambiguous case gets guessed at.
- **Cross-carrier comparisons are never auto-built.** Only a `draft_eligible` matrix — two `kind='bound'` terms of one policy — is built without a human.
- **Nothing is sent to a client.** The only outbound mail this plan adds is to the operator. No draft, comparison, or client communication is ever sent automatically.
- **Every stage is best-effort and separately re-runnable.** A stage that raises is logged and skipped; the document survives. This is the existing contract in `renewal/pipeline.py` and retry depends on it.
- **`RenewalRun` stays as a table and is never written again.** Comparisons built before this change reference it through `renewal_run_id`. This is the precedent `difference.prior_value` set: the column stays because it is the only record of what older rows meant.
- **Ids and hashes in logs, never document content.** The existing rule in `renewal/ingest.py` and `renewal/web/mail.py`.
- Run the suite with `.venv/bin/pytest`. It needs PostgreSQL; override the database with `TEST_DATABASE_URL`.
- Baseline before this plan starts: **726 tests passing**.

## A note on module naming

The spec says the state computation belongs in `renewal/stuck.py`, inherited from
the superseded stuck-documents spec. That module was never written, and the
computation now covers all three buckets rather than only the stuck ones, so this
plan puts it in **`renewal/inbox.py`**. Same module, same single responsibility,
a name that matches what it became.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `renewal/inbox.py` | Read-time state of one document, and the bucketed listing. No writes. |
| `renewal/background.py` | The in-process runner and the one function it runs. Owns `Document.status` transitions. |
| `renewal/notify.py` | The once-an-hour guard and the SMTP send. (The count itself lives in `renewal/inbox.py`, beside the listing that has to agree with it.) |
| `renewal/web/inbox.py` | `GET /`, `POST /documents`, `POST /documents/{id}/retry`, `GET /documents/{id}/review`, and the needs-you fixes. |
| `renewal/web/navbadge.py` | One middleware that puts the needs-you count on `request.state`. |
| `renewal/templates/inbox.html` | The three buckets. |
| `renewal/templates/inbox_detail.html` | One document: its fields, its corrections, its retry. |
| `migrations/versions/*_document_status.py` | `document.status`, `document.status_changed_at`. |
| `migrations/versions/*_notification_send.py` | `notification_send`. |
| `tests/test_inbox.py` | `state_of` and `inbox_rows`. |
| `tests/test_background.py` | Status transitions and failure handling. |
| `tests/test_notify.py` | Count, guard, SMTP failure. |
| `tests/test_web_inbox.py` | The inbox page, the drop box, retry, the detail view, the fixes. |
| `tests/test_auto_renewal.py` | The headline regression: email in, `premium_change` out, no human. |

**Modified:**

| File | Change |
|---|---|
| `renewal/models.py` | `Document.status`, `Document.status_changed_at`, `NotificationSend`. |
| `renewal/pipeline.py` | Split `run_stages` out of `ingest_document`; add `run_compare_stage`. |
| `renewal/comparison.py` | `build_comparison`'s `run_id` becomes optional. |
| `renewal/config.py` | SMTP and notification settings. |
| `renewal/web/__init__.py` | Mount the inbox router, delete the inline index, install the badge middleware, build the runner. |
| `renewal/web/deps.py` | Carry the runner. |
| `renewal/web/mail.py` | Attachments go to the background. |
| `renewal/web/clients.py` | Adopt `POST /clients` and `POST /policies`. |
| `renewal/web/unmatched.py` | Keep the two POST fixes, drop `GET /unmatched`. |
| `renewal/web/templating.py` | Context processor for the badge. |
| `renewal/templates/base.html` | Nav: Inbox with a badge, no Runs, no New renewal, no Unmatched. |
| `renewal/static/app.css` | Bucket and status styling. |
| `renewal/static/app.js` | Poll while anything is in flight. |

**Deleted:**

`renewal/web/runs.py`, `renewal/web/review.py` (renamed to `renewal/web/corrections.py`), `renewal/templates/run_new.html`, `renewal/templates/run_review.html`, `renewal/templates/index.html`, `renewal/templates/unmatched.html`, `tests/test_web_review.py`.

---

### Task 1: `Document.status` and `status_changed_at`

The inbox derives every bucket from rows that already exist, with one exception:
"working on it" has nothing to derive from. This is that exception, and nothing
else. `InboundMessage.processing_status` is the precedent, including its `CHECK`
constraint listing the allowed values.

`status_changed_at` exists so a retried document is not instantly stalled again.
Stalled means "in `processing` and nothing has happened since"; without this
column that would be measured from `uploaded_at`, which a retry does not move.

**Files:**
- Modify: `renewal/models.py:134-149` (the `Document` class)
- Create: `migrations/versions/<rev>_document_status.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Document.status` (`'processing' | 'processed' | 'failed'`, server default `'processed'`), `Document.status_changed_at` (`datetime`, server default `now()`).

- [x] **Step 1: Write the failing tests**

Append to `tests/test_models.py`:

```python
def test_a_document_is_processed_unless_something_says_otherwise(session, store):
    """The default is the finished state, not the in-flight one.

    Every path that creates a document synchronously — bulk import, the mail
    body document — is finished the moment it returns. Only the background
    path sets 'processing' explicitly, so the default needs no backfill for
    the rows that already exist.
    """
    from renewal.ingest import ingest_pdf
    from tests.pdfmaker import make_text_pdf

    document = ingest_pdf(
        session, store, data=make_text_pdf([["x"]]),
        original_filename="a.pdf", source="bulk_import", agency_id=1,
    )
    session.refresh(document)
    assert document.status == "processed"
    assert document.status_changed_at is not None


def test_an_unknown_document_status_is_rejected(session, store):
    import pytest
    from sqlalchemy.exc import IntegrityError

    from renewal.ingest import ingest_pdf
    from tests.pdfmaker import make_text_pdf

    document = ingest_pdf(
        session, store, data=make_text_pdf([["x"]]),
        original_filename="a.pdf", source="bulk_import", agency_id=1,
    )
    document.status = "thinking about it"
    with pytest.raises(IntegrityError):
        session.flush()
```

- [x] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_models.py -k "document_status or document_is_processed" -v`
Expected: FAIL — `AttributeError: 'Document' object has no attribute 'status'`

- [x] **Step 3: Add the columns to the model**

In `renewal/models.py`, replace the `Document` class body's opening (currently
`__tablename__ = "document"` immediately followed by `id:`) so the class reads:

```python
class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'processed', 'failed')",
            name="ck_document_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    blob_sha256: Mapped[str] = mapped_column(Text, index=True)
    original_filename: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer)
    has_text_layer: Mapped[bool] = mapped_column(Boolean)
    doc_type: Mapped[str] = mapped_column(Text)  # 'dec_page' in v0
    source: Mapped[str] = mapped_column(Text, server_default="manual_upload")
    # Only the in-flight fact. Every other thing the inbox shows is computed at
    # read time from rows the pipeline writes, because those change the moment
    # she acts and a stored copy would be stale immediately after the act that
    # fixed it.
    status: Mapped[str] = mapped_column(Text, server_default="processed")
    status_changed_at: Mapped[datetime] = _created_at()
    agency_id: Mapped[int | None] = mapped_column(
        ForeignKey("agency.id"), nullable=True
    )
    inbound_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_message.id"), nullable=True
    )
    uploaded_at: Mapped[datetime] = _created_at()
```

`CheckConstraint`, `Text`, `Integer`, `Boolean`, `ForeignKey`, `Mapped`,
`mapped_column`, `datetime` and `_created_at` are all already imported in this
module.

- [x] **Step 4: Write the migration**

Create `migrations/versions/c4a1f0b83d17_document_status.py`:

```python
"""document status

Revision ID: c4a1f0b83d17
Revises: 0bf79764eb52
Create Date: 2026-09-14 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a1f0b83d17'
down_revision: Union[str, Sequence[str], None] = '0bf79764eb52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 'processed' for every existing row: they were all ingested synchronously
    # and are finished. No backfill query is needed.
    op.add_column(
        "document",
        sa.Column("status", sa.Text(), nullable=False,
                  server_default="processed"),
    )
    op.add_column(
        "document",
        sa.Column("status_changed_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_check_constraint(
        "ck_document_status", "document",
        "status IN ('processing', 'processed', 'failed')",
    )
    # The inbox filters on it on every load.
    op.create_index("ix_document_status", "document", ["status"])


def downgrade() -> None:
    op.drop_index("ix_document_status", table_name="document")
    op.drop_constraint("ck_document_status", "document", type_="check")
    op.drop_column("document", "status_changed_at")
    op.drop_column("document", "status")
```

- [x] **Step 5: Run the tests to verify they pass**

The `engine` fixture drops the schema and runs `alembic upgrade head`, so the
migration is exercised by every test run.

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: PASS

- [x] **Step 6: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 728 passed (726 baseline + 2)

- [x] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/c4a1f0b83d17_document_status.py tests/test_models.py
git commit -m "feat(models): document status, the one thing the inbox cannot derive

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 2: Split `run_stages` out of `ingest_document`

A pure refactor with no behaviour change. The background task needs to run the
stages against a `Document` row that already exists — the row is written in the
request so the receipt appears instantly, and the stages run after the response.
Today those stages are welded to `ingest_pdf` inside one function.

**Files:**
- Modify: `renewal/pipeline.py:218-270`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `run_stages(session, store, document, *, extract_fields=True, model_client=None, settings=None) -> Document`
- `ingest_document` keeps its exact signature and behaviour.

- [x] **Step 1: Write the failing test**

Append to `tests/test_pipeline.py`:

```python
def test_run_stages_works_on_a_document_that_already_exists(session, store):
    """The background path's shape: the row is written in the request, the
    stages run afterwards against that row."""
    from renewal.ingest import ingest_pdf
    from renewal.pipeline import run_stages

    document = ingest_pdf(
        session, store, data=make_text_pdf([["Expiration Date: 07/01/2026"]]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
    )
    assert session.query(DocumentText).filter_by(
        document_id=document.id
    ).count() == 0

    run_stages(session, store, document)

    assert session.query(DocumentText).filter_by(
        document_id=document.id
    ).count() == 1


def test_run_stages_is_idempotent(session, store):
    """Retry depends on this. Every stage checks its own work first."""
    from renewal.ingest import ingest_pdf
    from renewal.pipeline import run_stages

    document = ingest_pdf(
        session, store, data=make_text_pdf([["Expiration Date: 07/01/2026"]]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
    )
    run_stages(session, store, document)
    run_stages(session, store, document)

    assert session.query(DocumentText).filter_by(
        document_id=document.id
    ).count() == 1
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_pipeline.py -k run_stages -v`
Expected: FAIL — `ImportError: cannot import name 'run_stages'`

- [x] **Step 3: Do the split**

In `renewal/pipeline.py`, replace the whole of `ingest_document` (lines 218-270)
with:

```python
def run_stages(
    session: Session,
    store: BlobStore,
    document: Document,
    *,
    extract_fields: bool = True,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
) -> Document:
    """Everything that happens to a document after it is stored.

    Split out of ingest_document so the background task can run it against a
    row that already exists: the row is written inside the request, so the
    receipt appears the instant the file lands, and these stages run after the
    response has gone out.

    Every stage is idempotent, which is what makes the Retry button safe.
    """
    run_text_stage(session, store, document)
    run_resolve_stage(session, document)
    # Optional rather than required: with no model configured the regex pass
    # still runs, and a caller with no model still gets the recall floor
    # instead of no dates at all.
    run_dates_stage(session, document, client=model_client, settings=settings)
    # After dates on purpose: date extraction must not be able to depend on a
    # label, and running it first makes that impossible rather than merely
    # untrue today.
    if model_client is not None and settings is not None:
        run_classify_stage(
            session, document, client=model_client, settings=settings
        )
    # Costs a model call per routed document, which is why the caller can turn
    # it off. Bulk import does; manual upload and email intake do not.
    if extract_fields and model_client is not None and settings is not None:
        run_fields_stage(
            session, store, document, client=model_client, settings=settings
        )
    # After the fields stage, because it promotes what that stage extracted,
    # and before attention, because Task 9's rules read the term it writes.
    if settings is not None:
        term = run_promote_stage(session, document, settings=settings)
        if term is not None:
            evaluate_promotion(session, term)
    # Last: its rules read the label and the link that the stages above wrote.
    if settings is not None:
        run_attention_stage(session, document, settings=settings)
    return document


def ingest_document(
    session: Session,
    store: BlobStore,
    *,
    data: bytes,
    original_filename: str,
    source: str,
    agency_id: int,
    inbound_message_id: int | None = None,
    extract_fields: bool = True,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
) -> Document:
    if source not in SOURCES:
        raise ValueError(f"unknown document source: {source!r}")
    document = ingest_pdf(
        session,
        store,
        data=data,
        original_filename=original_filename,
        source=source,
        agency_id=agency_id,
        inbound_message_id=inbound_message_id,
    )
    return run_stages(
        session,
        store,
        document,
        extract_fields=extract_fields,
        model_client=model_client,
        settings=settings,
    )
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pipeline.py -v`
Expected: PASS

- [x] **Step 5: Run the full suite — this is the refactor's real test**

Run: `.venv/bin/pytest`
Expected: 730 passed. Every existing pipeline, mail-intake and end-to-end test
goes through `ingest_document` and must be unchanged.

- [x] **Step 6: Commit**

```bash
git add renewal/pipeline.py tests/test_pipeline.py
git commit -m "refactor(pipeline): run_stages, so the stages can run off the request

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 3: The background runner

Two pieces: a runner that decides *where* work happens (a thread pool in the
app, inline in tests), and `process_document`, which is what gets run. The
runner owns every `Document.status` transition, and it must never leave a
document in `processing` — a status that outlives the work is exactly the
stalled state the spec makes visible, and it should only ever be caused by a
restart, never by an ordinary exception.

**Files:**
- Create: `renewal/background.py`
- Test: `tests/test_background.py`

**Interfaces:**
- Consumes: `run_stages` from Task 2, `Document.status` from Task 1.
- Produces:
  - `class InlineRunner` with `submit(fn, /, *args, **kwargs) -> None`
  - `class ThreadRunner` with `submit(fn, /, *args, **kwargs) -> None` and `shutdown() -> None`
  - `process_document(session_factory, store, document_id, *, model_client=None, settings=None) -> None`

- [x] **Step 1: Write the failing tests**

Create `tests/test_background.py`:

```python
"""The background path, and the one failure mode it is allowed to have.

In-process background work dies with the process. That is a real failure mode
and the design makes it visible rather than engineering it away — but it must
be the ONLY way a document is left in 'processing'. An ordinary exception has
to land on 'failed', which is a state the inbox explains, not a state that
looks like work still happening.
"""

from sqlalchemy.orm import sessionmaker

from renewal.background import InlineRunner, process_document
from renewal.ingest import ingest_pdf
from renewal.models import Document, DocumentText
from tests.pdfmaker import make_text_pdf


def _pending(engine, store) -> int:
    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, store, data=make_text_pdf([["Expiration Date: 07/01/2026"]]),
            original_filename="dec.pdf", source="manual_upload", agency_id=1,
        )
        document.status = "processing"
        session.commit()
        return document.id
    finally:
        session.close()


def test_processing_becomes_processed_and_the_stages_ran(engine, clean_db, store):
    document_id = _pending(engine, store)
    factory = sessionmaker(bind=engine)

    process_document(factory, store, document_id)

    session = factory()
    try:
        assert session.get(Document, document_id).status == "processed"
        assert session.query(DocumentText).filter_by(
            document_id=document_id
        ).count() == 1
    finally:
        session.close()


def test_a_raising_run_lands_on_failed_not_processing(
    engine, clean_db, store, monkeypatch
):
    """'processing' means a restart ate it. An exception is a different thing
    and has to say so."""
    import renewal.background as background

    def boom(*args, **kwargs):
        raise RuntimeError("the whole stage list exploded")

    monkeypatch.setattr(background, "run_stages", boom)
    document_id = _pending(engine, store)
    factory = sessionmaker(bind=engine)

    process_document(factory, store, document_id)

    session = factory()
    try:
        assert session.get(Document, document_id).status == "failed"
    finally:
        session.close()


def test_a_missing_document_is_not_an_error(engine, clean_db, store):
    """The row can be gone by the time the task runs. Nothing to do."""
    process_document(sessionmaker(bind=engine), store, 999_999)


def test_the_inline_runner_runs_it_now(engine, clean_db, store):
    """What the tests use, so a web test's assertions can run straight after
    the request instead of racing a thread."""
    seen = []
    InlineRunner().submit(seen.append, "ran")
    assert seen == ["ran"]


def test_status_changed_at_moves_with_the_status(engine, clean_db, store):
    """Stalled is measured from this, so a retry must reset the clock."""
    document_id = _pending(engine, store)
    factory = sessionmaker(bind=engine)

    session = factory()
    try:
        before = session.get(Document, document_id).status_changed_at
    finally:
        session.close()

    process_document(factory, store, document_id)

    session = factory()
    try:
        assert session.get(Document, document_id).status_changed_at > before
    finally:
        session.close()
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_background.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.background'`

- [x] **Step 3: Write `renewal/background.py`**

```python
"""Intake off the request thread.

No worker process, no queue, no scheduler. A thread pool inside the application
is the whole mechanism, which is the right size for an agency of a few people
and adds nothing to deploy.

It has one real failure mode: work in this process dies with this process, so a
restart can leave a document in 'processing' forever. The design makes that
visible — the inbox shows it as stalled with a Retry button — rather than
pretending it cannot happen. Every stage is idempotent, so retrying is safe by
construction.

An ordinary exception is NOT that failure mode and must not look like it. It
lands on 'failed', which the inbox explains and offers a retry for, because a
document that says 'working on it' forever while nothing is working is the one
outcome worse than a visible error.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import Document
from renewal.pipeline import run_stages
from renewal.providers import ModelClient

logger = logging.getLogger(__name__)


class InlineRunner:
    """Runs the work immediately, on the calling thread.

    What the tests use. A web test asserting on what intake produced would
    otherwise be racing a thread, and a test that sleeps to win that race is a
    test that fails on a slow machine.
    """

    def submit(self, fn, /, *args, **kwargs) -> None:
        fn(*args, **kwargs)


class ThreadRunner:
    """What the application uses.

    Two workers: intake is mostly waiting on a model provider, and a third
    concurrent extraction buys nothing for one person dropping in a file.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="intake"
        )

    def submit(self, fn, /, *args, **kwargs) -> None:
        self._pool.submit(fn, *args, **kwargs)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _set_status(session, document: Document, status: str) -> None:
    document.status = status
    document.status_changed_at = datetime.now(timezone.utc)
    session.commit()


def process_document(
    session_factory,
    store: BlobStore,
    document_id: int,
    *,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
) -> None:
    """Run the stages for one document and record where it ended up.

    Its own session: the request's session is closed by the time this runs.
    """
    session = session_factory()
    try:
        document = session.get(Document, document_id)
        if document is None:
            # The row can be gone by the time the task runs. Nothing to do,
            # and nothing wrong.
            logger.info("background skip document_id=%s reason=missing", document_id)
            return
        try:
            run_stages(
                session,
                store,
                document,
                model_client=model_client,
                settings=settings,
            )
            _set_status(session, document, "processed")
            logger.info("background done document_id=%s", document_id)
        except Exception:  # noqa: BLE001 - the status must never be left behind
            logger.exception("background failed document_id=%s", document_id)
            session.rollback()
            document = session.get(Document, document_id)
            if document is not None:
                _set_status(session, document, "failed")
    finally:
        session.close()
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_background.py -v`
Expected: PASS (5 tests)

- [x] **Step 5: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 735 passed

- [x] **Step 6: Commit**

```bash
git add renewal/background.py tests/test_background.py
git commit -m "feat(background): in-process intake with a visible failure mode

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---
### Task 4: What became of one document

The read-time state computation. This is where the superseded stuck-documents
spec's analysis lives: state is computed from rows the pipeline already wrote,
never stored, because it changes the moment she acts and a stored copy would be
stale immediately after the act that fixed it.

**The trap this task has to avoid**, named in that spec: a document that was
routed to field extraction but has no `Extraction` row is the bulk-import
default — extraction was switched off deliberately. It is *not-yet-processed*,
not stuck, and it must not appear in "needs you".

**Files:**
- Create: `renewal/inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: `Document.status` / `status_changed_at` (Task 1); `latest_link` from `renewal.resolve.service`; `should_extract_fields` and `latest_class` from `renewal.pipeline` / `renewal.classify.runner`; `unresolved_field_paths` from `renewal.promote`; `effective_values` from `renewal.corrections`.
- Produces:
  - `BUCKETS = ("working", "stalled", "needs_you", "done")`
  - `REASONS = ("failed", "needs_client", "needs_policy", "needs_review", "nothing_extracted")`
  - `@dataclass(frozen=True) class DocumentState` with fields `document, bucket, reason, summary, client_id, policy_id, comparison_id, extraction_id`
  - `state_of(session, document, *, stalled_after=STALLED_AFTER) -> DocumentState`
  - `STALLED_AFTER = timedelta(minutes=10)`

- [x] **Step 1: Write the failing tests**

Create `tests/test_inbox.py`:

```python
"""What became of a document, computed from what is recorded.

Nothing here writes. Every bucket is derived on read, so the state changes the
moment she acts rather than the next time something remembers to update a row.
"""

import json
from datetime import date, datetime, timedelta, timezone

from renewal.classify.runner import latest_class
from renewal.inbox import state_of
from renewal.ingest import ingest_pdf
from renewal.models import (
    Client, DocumentClassification, ExtractedField, Extraction, Policy,
    PolicyTerm,
)
from renewal.pipeline import ingest_document
from renewal.resolve.service import assign
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings

DEC_PAGE = [
    "PROGRESSIVE COMMERCIAL AUTO",
    "Policy Number: AU-4471",
    "Total Policy Premium $4,210.00",
    "Effective Date: 07/01/2026",
    "Expiration Date: 07/01/2027",
]


class Carrier:
    """One client for three stages, told apart by the model each one asks for."""

    def complete(self, *, model, system, content):
        settings = _settings()
        if model == settings.classification_model:
            return json.dumps({"doc_class": "declarations", "confidence": 0.95})
        if model == settings.date_model:
            return json.dumps({"dates": []})
        return json.dumps(
            {
                "fields": [
                    {
                        "field_path": path,
                        "value": value,
                        "confidence": 0.97,
                        "source_page": 1,
                        "source_text": source,
                    }
                    for path, value, source in (
                        ("policy.total_premium", "4210.00",
                         "Total Policy Premium $4,210.00"),
                        ("policy.policy_number", "AU-4471",
                         "Policy Number: AU-4471"),
                        ("policy.effective_date", "2026-07-01",
                         "Effective Date: 07/01/2026"),
                        ("policy.expiration_date", "2027-07-01",
                         "Expiration Date: 07/01/2027"),
                    )
                ]
            }
        )


def _incumbent(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="commercial_auto",
        state="OR",
    )
    session.add(policy)
    session.flush()
    prior = PolicyTerm(
        policy_id=policy.id, kind="bound", carrier_name="Progressive",
        policy_number="AU-4471", effective_date=date(2025, 7, 1),
        expiration_date=date(2026, 7, 1), total_premium="3900.00",
    )
    session.add(prior)
    session.flush()
    return client, policy, prior


def _bare(session, store, filename="a.pdf"):
    return ingest_pdf(
        session, store, data=make_text_pdf([["nothing recognisable here"]]),
        original_filename=filename, source="manual_upload", agency_id=1,
    )


def test_processing_is_working_on_it(session, store):
    document = _bare(session, store)
    document.status = "processing"
    document.status_changed_at = datetime.now(timezone.utc)
    session.flush()
    assert state_of(session, document).bucket == "working"


def test_processing_for_too_long_is_stalled(session, store):
    """The one failure mode in-process background work has, made visible."""
    document = _bare(session, store)
    document.status = "processing"
    document.status_changed_at = datetime.now(timezone.utc) - timedelta(hours=3)
    session.flush()
    assert state_of(session, document).bucket == "stalled"


def test_a_failed_run_needs_her(session, store):
    document = _bare(session, store)
    document.status = "failed"
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "failed")


def test_no_link_at_all_needs_a_client(session, store):
    """Today's /unmatched, moved."""
    document = _bare(session, store)
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "needs_client")


def test_a_client_but_no_policy_needs_a_policy(session, store):
    client, _, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    # Overwrite the auto link with a client-only one: matched to a client,
    # no policy number it could attach to.
    assign(session, document.id, client_id=client.id, policy_id=None,
           candidates=[])
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "needs_policy")
    assert state.client_id == client.id


def test_a_document_that_does_not_route_is_done(session, store):
    """An invoice is stored and searchable. No term was ever expected, and
    saying 'needs you' about it would be a lie."""
    client, _, _ = _incumbent(session)
    document = _bare(session, store)
    assign(session, document.id, client_id=client.id, policy_id=None,
           candidates=[])
    session.add(
        DocumentClassification(
            document_id=document.id, doc_class="invoice", confidence=0.9,
            classifier_version="v1", model_id="test",
        )
    )
    session.flush()
    state = state_of(session, document)
    assert state.bucket == "done"
    assert "no policy term expected" in state.summary


def test_routed_but_never_extracted_is_not_stuck(session, store):
    """The bulk-import default, and the trap the stuck-documents spec named.

    Extraction was switched off deliberately for this document. It is
    not-yet-processed, not stuck, and putting it in 'needs you' would fill the
    queue with an entire archive nobody asked about.
    """
    client, policy, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
        extract_fields=False, model_client=Carrier(), settings=_settings(),
    )
    assert latest_class(session, document.id) == "declarations"
    assert session.query(Extraction).filter_by(
        document_id=document.id
    ).count() == 0
    assert state_of(session, document).bucket == "done"


def test_a_flagged_field_needs_review(session, store):
    client, policy, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    extraction = session.query(Extraction).filter_by(
        document_id=document.id
    ).one()
    field = session.query(ExtractedField).filter_by(
        extraction_id=extraction.id, field_path="policy.total_premium"
    ).one()
    field.needs_review = True
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "needs_review")
    assert state.extraction_id == extraction.id


def test_an_extraction_that_produced_nothing_says_so(session, store):
    client, policy, _ = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="manual_upload", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    extraction = session.query(Extraction).filter_by(
        document_id=document.id
    ).one()
    session.query(ExtractedField).filter_by(
        extraction_id=extraction.id
    ).delete()
    session.flush()
    state = state_of(session, document)
    assert (state.bucket, state.reason) == ("needs_you", "nothing_extracted")


def test_a_promoted_term_is_done_and_says_what_it_became(session, store):
    client, policy, prior = _incumbent(session)
    document = ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="dec.pdf", source="email_attachment", agency_id=1,
        model_client=Carrier(), settings=_settings(),
    )
    term = session.query(PolicyTerm).filter_by(
        source_document_id=document.id
    ).one()
    state = state_of(session, document)
    assert state.bucket == "done"
    assert "AU-4471" in state.summary
    assert state.policy_id == policy.id
    assert term.kind == "bound"
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_inbox.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.inbox'`

- [x] **Step 3: Write `renewal/inbox.py`**

```python
"""What became of a document.

Nothing here writes. Every bucket is computed from rows the pipeline already
wrote, on every read, because the state changes the moment she acts on it — a
stored copy would be stale immediately after the act that fixed it, and the one
thing worse than no status is a status that lies.

The single exception is Document.status, which records that work is in flight.
That has nothing to derive from: an unprocessed document and a document being
processed right now look identical in every other table.

Order matters below. The checks run from "we know least" to "we know most", so
the reason on the row is the earliest thing that stopped it rather than the
last thing that noticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.classify.runner import latest_class
from renewal.corrections import effective_values
from renewal.models import (
    Comparison, ComparisonColumn, Document, Extraction, Policy, PolicyTerm,
)
from renewal.pipeline import should_extract_fields
from renewal.promote import unresolved_field_paths
from renewal.resolve.service import latest_link

BUCKETS = ("working", "stalled", "needs_you", "done")

REASONS = (
    "failed",
    "needs_client",
    "needs_policy",
    "needs_review",
    "nothing_extracted",
)

# Long enough that a slow provider call is not mistaken for a dead process,
# short enough that she is not staring at a spinner for an afternoon. OCR plus
# three model calls is the worst realistic case and runs well under this.
STALLED_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class DocumentState:
    document: Document
    bucket: str
    reason: str | None
    summary: str
    client_id: int | None
    policy_id: int | None
    comparison_id: int | None
    extraction_id: int | None


def _latest_extraction(session: Session, document_id: int) -> Extraction | None:
    return (
        session.query(Extraction)
        .filter_by(document_id=document_id)
        .order_by(Extraction.id.desc())
        .first()
    )


def _comparison_for(session: Session, term_id: int) -> int | None:
    """The newest comparison this term appears in, whichever column it is."""
    return session.scalar(
        select(ComparisonColumn.comparison_id)
        .where(ComparisonColumn.policy_term_id == term_id)
        .order_by(ComparisonColumn.comparison_id.desc())
        .limit(1)
    )


def _state(
    document: Document,
    bucket: str,
    summary: str,
    *,
    reason: str | None = None,
    client_id: int | None = None,
    policy_id: int | None = None,
    comparison_id: int | None = None,
    extraction_id: int | None = None,
) -> DocumentState:
    return DocumentState(
        document=document,
        bucket=bucket,
        reason=reason,
        summary=summary,
        client_id=client_id,
        policy_id=policy_id,
        comparison_id=comparison_id,
        extraction_id=extraction_id,
    )


def state_of(
    session: Session,
    document: Document,
    *,
    stalled_after: timedelta = STALLED_AFTER,
) -> DocumentState:
    if document.status == "processing":
        changed = document.status_changed_at
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - changed > stalled_after:
            return _state(
                document, "stalled",
                "Started but never finished — the application restarted while "
                "this was in flight.",
            )
        return _state(document, "working", "Reading this now.")

    if document.status == "failed":
        return _state(
            document, "needs_you", "Processing failed. Retry, or open it to "
            "enter the fields by hand.",
            reason="failed",
        )

    link = latest_link(session, document.id)
    if link is None:
        return _state(
            document, "needs_you", "Not filed yet — who is this for?",
            reason="needs_client",
        )

    # Before the policy check on purpose. A document that never routes to field
    # extraction is not waiting for a policy: no term was ever expected of it,
    # and asking her to attach an invoice to a policy is asking for a decision
    # that means nothing.
    if not should_extract_fields(session, document.id):
        label = latest_class(session, document.id) or "unclassified"
        return _state(
            document, "done",
            f"Stored and searchable — {label.replace('_', ' ')}, "
            "no policy term expected.",
            client_id=link.client_id, policy_id=link.policy_id,
        )

    if link.policy_id is None:
        return _state(
            document, "needs_you", "Filed to a client — which policy?",
            reason="needs_policy", client_id=link.client_id,
        )

    extraction = _latest_extraction(session, document.id)
    if extraction is None:
        # The bulk-import default: routed, but extraction was switched off for
        # this document on purpose. Not-yet-processed is not stuck, and putting
        # an entire imported archive in the queue would bury the real rows.
        return _state(
            document, "done",
            "Stored and searchable — fields were not extracted.",
            client_id=link.client_id, policy_id=link.policy_id,
        )

    if unresolved_field_paths(session, extraction.id):
        return _state(
            document, "needs_you", "Some fields need checking before this can "
            "be filed.",
            reason="needs_review", client_id=link.client_id,
            policy_id=link.policy_id, extraction_id=extraction.id,
        )

    if not effective_values(session, extraction.id):
        return _state(
            document, "needs_you", "Nothing could be read from this.",
            reason="nothing_extracted", client_id=link.client_id,
            policy_id=link.policy_id, extraction_id=extraction.id,
        )

    term = (
        session.query(PolicyTerm)
        .filter_by(promoted_from_extraction_id=extraction.id)
        .first()
    )
    if term is None:
        # Clean, linked, and still not promoted: the twin check stopped it,
        # because these exact bytes are already filed against this policy. A
        # resend is not a problem and must not read like one.
        return _state(
            document, "done",
            "Already filed — an identical document is on this policy.",
            client_id=link.client_id, policy_id=link.policy_id,
            extraction_id=extraction.id,
        )

    policy = session.get(Policy, term.policy_id)
    number = policy.policy_number if policy is not None else "this policy"
    comparison_id = _comparison_for(session, term.id)
    if comparison_id is not None:
        summary = f"Renewal term on {number} — compared with the prior term."
    else:
        summary = f"Term on {number} — filed."
    return _state(
        document, "done", summary,
        client_id=link.client_id, policy_id=link.policy_id,
        comparison_id=comparison_id, extraction_id=extraction.id,
    )
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_inbox.py -v`
Expected: PASS (10 tests)

- [x] **Step 5: Check the import direction**

`renewal/inbox.py` imports from `renewal.pipeline`. Nothing in `renewal.pipeline`
may import `renewal.inbox`, or the cycle closes.

Run: `.venv/bin/python -c "import renewal.inbox, renewal.pipeline; print('ok')"`
Expected: `ok`

- [x] **Step 6: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 745 passed

- [x] **Step 7: Commit**

```bash
git add renewal/inbox.py tests/test_inbox.py
git commit -m "feat(inbox): what became of a document, computed on read

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 5: The bucketed listing

**Files:**
- Modify: `renewal/inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: `state_of`, `DocumentState` (Task 4).
- Produces:
  - `inbox_rows(session, *, stalled_after=STALLED_AFTER, limit=200) -> list[DocumentState]` — newest first
  - `bucketed(states) -> dict[str, list[DocumentState]]` — keys `"working"` (stalled folded in, stalled first), `"needs_you"`, `"done"`
  - `needs_you_count(session) -> int`
  - `anything_in_flight(session) -> bool`

`needs_you_count` lives here rather than in `renewal/notify.py` because it is the
same computation the page does, and two copies of "what counts as needing her"
would disagree the first time either changed.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_inbox.py`:

```python
def test_rows_come_back_newest_first(session, store):
    first = _bare(session, store, "first.pdf")
    second = _bare(session, store, "second.pdf")
    from renewal.inbox import inbox_rows

    rows = inbox_rows(session)
    assert [r.document.id for r in rows] == [second.id, first.id]


def test_buckets_put_stalled_at_the_top_of_working(session, store):
    from renewal.inbox import bucketed, inbox_rows

    fresh = _bare(session, store, "fresh.pdf")
    fresh.status = "processing"
    fresh.status_changed_at = datetime.now(timezone.utc)
    old = _bare(session, store, "old.pdf")
    old.status = "processing"
    old.status_changed_at = datetime.now(timezone.utc) - timedelta(hours=3)
    session.flush()

    groups = bucketed(inbox_rows(session))
    assert [s.document.id for s in groups["working"]] == [old.id, fresh.id]
    assert groups["working"][0].bucket == "stalled"


def test_the_needs_you_count_is_what_the_page_shows(session, store):
    from renewal.inbox import bucketed, inbox_rows, needs_you_count

    _bare(session, store, "a.pdf")
    _bare(session, store, "b.pdf")
    session.flush()

    assert needs_you_count(session) == 2
    assert len(bucketed(inbox_rows(session))["needs_you"]) == 2


def test_nothing_in_flight_when_nothing_is_processing(session, store):
    from renewal.inbox import anything_in_flight

    document = _bare(session, store)
    session.flush()
    assert not anything_in_flight(session)

    document.status = "processing"
    session.flush()
    assert anything_in_flight(session)
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_inbox.py -k "newest_first or buckets_put or needs_you_count or in_flight" -v`
Expected: FAIL — `ImportError: cannot import name 'inbox_rows'`

- [x] **Step 3: Append to `renewal/inbox.py`**

```python
def inbox_rows(
    session: Session,
    *,
    stalled_after: timedelta = STALLED_AFTER,
    limit: int = 200,
) -> list[DocumentState]:
    """Every document, newest first, with what became of it.

    Capped rather than paged: past a couple of hundred rows the inbox is not
    the right screen any more and search is. The cap is here so a bulk-imported
    archive cannot turn the front door into a several-second query.
    """
    documents = session.scalars(
        select(Document).order_by(Document.id.desc()).limit(limit)
    )
    return [
        state_of(session, document, stalled_after=stalled_after)
        for document in documents
    ]


def bucketed(states: list[DocumentState]) -> dict[str, list[DocumentState]]:
    """Three buckets on the page, four states underneath.

    Stalled is not its own bucket: it is the same answer to "is this finished?"
    as working on it, with a different explanation and a button. Sorting it to
    the top of that bucket puts the one row that needs her first.
    """
    groups: dict[str, list[DocumentState]] = {
        "working": [], "needs_you": [], "done": [],
    }
    for state in states:
        key = "working" if state.bucket in ("working", "stalled") else state.bucket
        groups[key].append(state)
    groups["working"].sort(key=lambda s: s.bucket != "stalled")
    return groups


def needs_you_count(session: Session, *, limit: int = 200) -> int:
    """The number beside the nav link, and what the notification email counts.

    Computed the same way the page computes it, in one place, so the badge and
    the list cannot disagree.
    """
    return sum(
        1 for state in inbox_rows(session, limit=limit)
        if state.bucket == "needs_you"
    )


def anything_in_flight(session: Session) -> bool:
    """Whether the page should keep polling."""
    return session.scalar(
        select(Document.id).where(Document.status == "processing").limit(1)
    ) is not None
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_inbox.py -v`
Expected: PASS (14 tests)

- [x] **Step 5: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 749 passed

- [x] **Step 6: Commit**

```bash
git add renewal/inbox.py tests/test_inbox.py
git commit -m "feat(inbox): the bucketed listing and the needs-you count

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---
### Task 6: The inbox page

`GET /` stops being a list of runs and becomes the front door: every document,
newest first, in three buckets. Read-only in this task — the drop box is Task 7,
retry is Task 8, and the inline fixes are Task 10.

**Files:**
- Create: `renewal/web/inbox.py`
- Create: `renewal/templates/inbox.html`
- Modify: `renewal/web/__init__.py:20-33,74-86`
- Modify: `renewal/templates/base.html:29`
- Modify: `renewal/static/app.css`
- Delete: `renewal/templates/index.html`
- Test: `tests/test_web_inbox.py`

**Interfaces:**
- Consumes: `inbox_rows`, `bucketed`, `anything_in_flight` (Task 5).
- Produces: `renewal.web.inbox.register(app, deps)`; the template context `{"groups": dict[str, list[DocumentState]], "in_flight": bool}`.

- [x] **Step 1: Write the failing test**

Create `tests/test_web_inbox.py`:

```python
"""The front door.

One screen that says what arrived, what happened to it, and what is left for
her. Every assertion here is about what she can see without knowing anything
about the pipeline underneath.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.ingest import ingest_pdf
from renewal.models import Client, Document, Policy, PolicyTerm
from renewal.resolve.service import assign
from renewal.web import create_app
from tests.authhelp import sign_in
from tests.pdfmaker import make_text_pdf


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
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def signed(app, engine):
    @contextmanager
    def _open():
        with TestClient(app) as test_client:
            sign_in(test_client, engine)
            yield test_client

    return _open


def _document(engine, settings, *, filename="a.pdf", status="processed",
              age=timedelta(0)):
    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["nothing recognisable here"]]),
            original_filename=filename, source="manual_upload", agency_id=1,
        )
        document.status = status
        document.status_changed_at = datetime.now(timezone.utc) - age
        session.commit()
        return document.id
    finally:
        session.close()


def test_the_inbox_lists_a_document_that_needs_a_client(
    signed, engine, settings
):
    _document(engine, settings, filename="mystery.pdf")
    with signed() as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "mystery.pdf" in page.text
    assert "who is this for" in page.text


def test_a_processing_document_shows_as_working_on_it(signed, engine, settings):
    _document(engine, settings, filename="inflight.pdf", status="processing")
    with signed() as client:
        page = client.get("/")
    assert "Reading this now" in page.text


def test_a_long_running_document_shows_as_stalled(signed, engine, settings):
    _document(engine, settings, filename="stuck.pdf", status="processing",
              age=timedelta(hours=3))
    with signed() as client:
        page = client.get("/")
    assert "restarted while this was in flight" in page.text


def test_an_empty_inbox_says_so_rather_than_showing_three_empty_headings(
    signed,
):
    with signed() as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "Nothing has come in yet" in page.text


def test_the_inbox_no_longer_lists_runs(signed):
    with signed() as client:
        page = client.get("/")
    assert "Renewal runs" not in page.text
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_web_inbox.py -v`
Expected: FAIL — the index still renders "Renewal runs" and no document rows.

- [x] **Step 3: Write `renewal/web/inbox.py`**

```python
"""The front door.

Replaces the run list. Every document that has arrived, newest first, grouped
by whether anything is left to do about it. The grouping is computed in
renewal/inbox.py; this module parses the request and renders.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from renewal.inbox import anything_in_flight, bucketed, inbox_rows
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES


def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def inbox(request: Request):
        with session_factory() as session:
            groups = bucketed(inbox_rows(session))
            return TEMPLATES.TemplateResponse(
                request,
                "inbox.html",
                {"groups": groups, "in_flight": anything_in_flight(session)},
            )

    app.include_router(router)
```

- [x] **Step 4: Write `renewal/templates/inbox.html`**

```jinja
{% extends "base.html" %}
{% block title %}Inbox{% endblock %}
{% block content %}
{% set total = groups.working | length + groups.needs_you | length + groups.done | length %}
<div class="pagehead">
  <h1>Inbox</h1>
  {% if total %}<span class="sub">{{ total }} document{{ "" if total == 1 else "s" }}, newest first</span>{% endif %}
</div>

{% if not total %}
<div class="empty">
  <p>Nothing has come in yet.</p>
  <p>
    Documents arrive on their own when a carrier emails them. Anything that
    lands here is read, filed, and compared without being asked — and anything
    that genuinely needs you says so.
  </p>
</div>
{% else %}

{# Working on it first: it is the only bucket that changes on its own, and
   burying it under a long Done list means she never sees it move. #}
{% if groups.working %}
<section class="panel bucket" data-bucket="working">
  <header><h2>Working on it</h2><span class="sub muted">{{ groups.working | length }}</span></header>
  <ul class="inbox">
    {% for state in groups.working %}
    <li data-state="{{ state.bucket }}">
      <span class="path"><a href="/documents/{{ state.document.id }}">{{ state.document.original_filename }}</a></span>
      <span class="muted">{{ state.summary }}</span>
    </li>
    {% endfor %}
  </ul>
</section>
{% endif %}

{% if groups.needs_you %}
<section class="panel bucket" data-bucket="needs-you">
  <header><h2>Needs you</h2><span class="sub muted">{{ groups.needs_you | length }}</span></header>
  <ul class="inbox">
    {% for state in groups.needs_you %}
    <li data-state="needs_you" data-reason="{{ state.reason }}">
      <span class="path"><a href="/documents/{{ state.document.id }}">{{ state.document.original_filename }}</a></span>
      <span>{{ state.summary }}</span>
    </li>
    {% endfor %}
  </ul>
</section>
{% endif %}

{% if groups.done %}
<section class="panel bucket" data-bucket="done">
  <header><h2>Done</h2><span class="sub muted">{{ groups.done | length }}</span></header>
  <ul class="inbox">
    {% for state in groups.done %}
    <li data-state="done">
      <span class="path"><a href="/documents/{{ state.document.id }}">{{ state.document.original_filename }}</a></span>
      {# The row says what it became, not merely that it finished. This is how
         she knows the automatic part worked without visiting anything. #}
      {% if state.comparison_id %}
      <span><a href="/comparisons/{{ state.comparison_id }}">{{ state.summary }}</a></span>
      {% else %}
      <span class="muted">{{ state.summary }}</span>
      {% endif %}
    </li>
    {% endfor %}
  </ul>
</section>
{% endif %}

{% endif %}
{% endblock %}
```

- [x] **Step 5: Mount it and delete the inline index**

In `renewal/web/__init__.py`:

Replace the import block and `ROUTER_MODULES` (lines 20-33) with:

```python
from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.web import (
    attention as attention_routes, auth as auth_routes, calendar,
    clients as client_routes, comparison, inbox as inbox_routes,
    mail as mail_routes, review, runs, search as search_routes,
    settings as settings_routes, unmatched,
)
from renewal.web import security
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

ROUTER_MODULES = (
    inbox_routes, runs, review, comparison, unmatched, calendar,
    settings_routes, search_routes, client_routes, attention_routes,
    auth_routes,
)
```

Note the `Client, Policy, RenewalRun` import from `renewal.models` is deleted —
nothing in this module reads them once the inline index is gone.

Then delete the whole `@app.get("/")` index function (lines 74-86).

- [x] **Step 6: Rename the nav entry**

In `renewal/templates/base.html`, replace line 29:

```jinja
      <a href="/" {% if path == "/" %}aria-current="page"{% endif %}>Runs</a>
```

with:

```jinja
      <a href="/" {% if path == "/" %}aria-current="page"{% endif %}>Inbox</a>
```

Leave the "New renewal" and "Unmatched" links alone — they are removed in Tasks
7 and 10, once what replaces each of them exists.

- [x] **Step 7: Add the bucket styles**

Append to `renewal/static/app.css`:

```css
/* The inbox. One list shape for three buckets, because the difference between
   them is what the row says, not how it looks. */
.bucket > header {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
}

ul.inbox {
  list-style: none;
  margin: 0;
  padding: 0;
}

ul.inbox > li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.25rem 0.9rem;
  padding: 0.55rem 0;
  border-top: 1px solid var(--rule);
}

ul.inbox > li:first-child {
  border-top: 0;
}

ul.inbox > li > .path {
  flex: 0 1 18rem;
  min-width: 0;
  overflow-wrap: anywhere;
}

/* Stalled is the one row in this bucket that is not going to fix itself. */
ul.inbox > li[data-state="stalled"] {
  border-left: 3px solid var(--warn);
  padding-left: 0.6rem;
}
```

If `--rule` or `--warn` are not defined in this stylesheet's custom-property
block, use the nearest existing equivalents — check the `:root` block at the top
of `app.css` and substitute rather than inventing new variables.

- [x] **Step 8: Delete the old index template**

```bash
git rm renewal/templates/index.html
```

- [x] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_inbox.py -v`
Expected: PASS (5 tests)

- [x] **Step 10: Run the full suite and fix what the index change broke**

Run: `.venv/bin/pytest`
Expected: 754 passed. Any test asserting on the old run list fails here; find
them with `grep -rn "Renewal runs\|index.html" tests/` and update the assertion
to the inbox's text rather than deleting the test.

- [x] **Step 11: Commit**

```bash
git add -A renewal/web/__init__.py renewal/web/inbox.py renewal/templates/ renewal/static/app.css tests/test_web_inbox.py
git commit -m "feat(web): the inbox replaces the run list as the front door

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 7: The drop box, and intake in the background

The manual backup path: one file, one button, no policy picker and no second
slot. The request stores the file, writes the row, and answers. The stages run
after the response — which also fixes a latent bug in the mail webhook that
Task 11 finishes: real providers time out around 10-30 seconds and retry, and
inbound mail currently runs OCR plus up to three model calls inside the webhook.

**Files:**
- Modify: `renewal/web/deps.py`
- Modify: `renewal/web/inbox.py`
- Modify: `renewal/web/__init__.py` (build the runner)
- Modify: `renewal/templates/inbox.html`
- Modify: `renewal/templates/base.html` (drop "New renewal")
- Modify: `renewal/static/app.js` (poll while anything is in flight)
- Test: `tests/test_web_inbox.py`

**Interfaces:**
- Consumes: `InlineRunner`, `ThreadRunner`, `process_document` (Task 3).
- Produces: `Deps.runner`; `create_app(..., runner=None)`; `POST /documents` accepting one `UploadFile` named `document`, redirecting 303 to `/`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_web_inbox.py`:

```python
def test_dropping_a_file_in_files_it_and_answers_immediately(
    signed, engine, settings
):
    """The receipt. The row exists the instant the file lands, before any
    processing has happened — that is the confirmation that it arrived."""
    with signed() as client:
        response = client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    session = sessionmaker(bind=engine)()
    try:
        document = session.query(Document).one()
        assert document.original_filename == "dropped.pdf"
        assert document.source == "manual_upload"
    finally:
        session.close()


def test_the_dropped_file_appears_on_the_inbox(signed, engine, settings):
    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )
        page = client.get("/")
    assert "dropped.pdf" in page.text


def test_the_stages_run_and_the_status_settles(signed, engine, settings):
    """With the inline runner the stages have finished by the time the
    response comes back, which is what lets this assert instead of sleep."""
    from renewal.models import DocumentText

    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )

    session = sessionmaker(bind=engine)()
    try:
        document = session.query(Document).one()
        assert document.status == "processed"
        assert session.query(DocumentText).filter_by(
            document_id=document.id
        ).count() == 1
    finally:
        session.close()


def test_a_file_that_is_not_a_pdf_is_refused_with_a_reason(signed):
    with signed() as client:
        response = client.post(
            "/documents",
            files={"document": ("notes.txt", b"just some text", "text/plain")},
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "PDF" in response.text
```

And change the `app` fixture to use the inline runner:

```python
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
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_web_inbox.py -k "dropping or dropped or stages_run or not_a_pdf" -v`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'runner'`

- [x] **Step 3: Carry the runner on `Deps`**

In `renewal/web/deps.py`, add one field:

```python
@dataclass(frozen=True)
class Deps:
    settings: Settings
    store: BlobStore
    model_client: Any
    session_factory: Any
    # Where work that must not block the response goes. The application builds
    # a ThreadRunner; tests pass an InlineRunner so their assertions are not
    # racing a thread.
    runner: Any = None
```

- [x] **Step 4: Build the runner in `create_app`**

In `renewal/web/__init__.py`, change the signature and the `Deps` construction:

```python
def create_app(
    *, settings: Settings, store: BlobStore, model_client, session_factory,
    inbound_provider=None, runner=None,
):
```

and:

```python
    deps = Deps(
        settings=settings,
        store=store,
        model_client=model_client,
        session_factory=session_factory,
        runner=runner if runner is not None else ThreadRunner(),
    )
```

with the import at the top of the module:

```python
from renewal.background import ThreadRunner
```

- [x] **Step 5: Add the drop box route**

In `renewal/web/inbox.py`, add the imports and the route:

```python
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from renewal.background import process_document
from renewal.ingest import ingest_pdf

AGENCY_ID = 1

# Checked by content rather than by the filename or the browser's guess at the
# content type, both of which are supplied by whoever is uploading.
PDF_MAGIC = b"%PDF-"
```

```python
    @router.post("/documents")
    async def drop_document(document: UploadFile):
        """The manual backup path. One file, no questions.

        Everything the old two-upload form asked for — which policy, which
        slot, is this really the same file — is either answered by the pipeline
        or asked later on the row that needs it.
        """
        data = await document.read()
        if not data.startswith(PDF_MAGIC):
            raise HTTPException(
                status_code=400,
                detail="That file is not a PDF. Drop the PDF the carrier sent.",
            )

        with session_factory() as session:
            row = ingest_pdf(
                session,
                store,
                data=data,
                original_filename=document.filename or "dropped.pdf",
                source="manual_upload",
                agency_id=AGENCY_ID,
            )
            # Written before the stages run, so the row — the receipt that the
            # file arrived — is on the page the redirect lands on.
            row.status = "processing"
            session.commit()
            document_id = row.id

        deps.runner.submit(
            process_document,
            session_factory,
            store,
            document_id,
            model_client=model_client,
            settings=settings,
        )
        return RedirectResponse("/", status_code=303)
```

and pull `store`, `model_client` and `settings` off `deps` at the top of
`register`, alongside the existing `session_factory`:

```python
def register(app, deps: Deps) -> None:
    session_factory = deps.session_factory
    store = deps.store
    settings = deps.settings
    model_client = deps.model_client
    router = APIRouter()
```

- [x] **Step 6: Put the drop box on the page**

In `renewal/templates/inbox.html`, insert immediately after the `pagehead` div:

```jinja
{# The backup path, not the main one. Deliberately small: mail is how
   documents are meant to arrive, and a big upload box on the front door
   suggests otherwise. #}
<form class="dropbox" method="post" action="/documents" enctype="multipart/form-data">
  <label for="dropbox-file">Got one by hand?</label>
  <input id="dropbox-file" type="file" name="document" accept="application/pdf" required>
  <button class="btn" type="submit">Add it</button>
</form>
```

and append to `renewal/static/app.css`:

```css
.dropbox {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.6rem;
  margin: 0 0 1.2rem;
  font-size: 0.92rem;
}
```

- [x] **Step 7: Drop "New renewal" from the nav**

In `renewal/templates/base.html`, delete line 30 entirely:

```jinja
      <a href="/runs/new" {% if path == "/runs/new" %}aria-current="page"{% endif %}>New renewal</a>
```

- [x] **Step 8: Poll while anything is in flight**

Append to `renewal/static/app.js`:

```javascript
// The inbox refreshes itself while something is still being read, and stops
// the moment nothing is. A page that polls forever is a page that burns a
// query every few seconds on an idle tab all afternoon.
(function () {
  var page = document.querySelector('[data-poll="inbox"]');
  if (!page) return;
  window.setTimeout(function () {
    window.location.reload();
  }, 4000);
})();
```

and in `renewal/templates/inbox.html`, make the wrapper carry the flag — change
the `pagehead` div's opening tag to:

```jinja
<div class="pagehead" {% if in_flight %}data-poll="inbox"{% endif %}>
```

- [x] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_inbox.py -v`
Expected: PASS (9 tests)

- [x] **Step 10: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 758 passed

- [x] **Step 11: Commit**

```bash
git add renewal/web/ renewal/templates/ renewal/static/ tests/test_web_inbox.py
git commit -m "feat(web): one-file drop box, with intake off the request thread

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 8: Retry

The stalled state's fix. Every stage checks its own work before doing it, so a
retry of a document that actually finished is a no-op rather than a second
extraction — which is what makes this button safe to offer on any row.

**Files:**
- Modify: `renewal/web/inbox.py`
- Modify: `renewal/templates/inbox.html`
- Test: `tests/test_web_inbox.py`

**Interfaces:**
- Produces: `POST /documents/{document_id}/retry`, redirecting 303 to `/`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_web_inbox.py`:

```python
def test_retry_reruns_a_stalled_document(signed, engine, settings):
    from renewal.models import DocumentText

    document_id = _document(engine, settings, filename="stuck.pdf",
                            status="processing", age=timedelta(hours=3))
    with signed() as client:
        response = client.post(f"/documents/{document_id}/retry",
                               follow_redirects=False)
    assert response.status_code == 303

    session = sessionmaker(bind=engine)()
    try:
        assert session.get(Document, document_id).status == "processed"
        assert session.query(DocumentText).filter_by(
            document_id=document_id
        ).count() == 1
    finally:
        session.close()


def test_retrying_a_finished_document_changes_nothing(signed, engine, settings):
    """Idempotent by construction: every stage checks its own work first."""
    from renewal.models import DocumentText

    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )

    session = sessionmaker(bind=engine)()
    try:
        document_id = session.query(Document).one().id
    finally:
        session.close()

    with signed() as client:
        client.post(f"/documents/{document_id}/retry", follow_redirects=False)

    session = sessionmaker(bind=engine)()
    try:
        assert session.query(DocumentText).filter_by(
            document_id=document_id
        ).count() == 1
    finally:
        session.close()


def test_retrying_a_document_that_does_not_exist_is_a_404(signed):
    with signed() as client:
        response = client.post("/documents/999999/retry",
                               follow_redirects=False)
    assert response.status_code == 404


def test_a_stalled_row_offers_the_retry_button(signed, engine, settings):
    _document(engine, settings, filename="stuck.pdf", status="processing",
              age=timedelta(hours=3))
    with signed() as client:
        page = client.get("/")
    assert "/retry" in page.text
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_web_inbox.py -k retry -v`
Expected: FAIL — 405 Method Not Allowed on the retry path

- [x] **Step 3: Add the route**

In `renewal/web/inbox.py`:

```python
    @router.post("/documents/{document_id}/retry")
    def retry_document(document_id: int):
        """Re-run the stages for one document.

        Safe on anything: every stage checks its own work before doing it, and
        promotion is idempotent per extraction and per blob. A retry of a
        document that already finished writes nothing.
        """
        with session_factory() as session:
            row = session.get(Document, document_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such document")
            row.status = "processing"
            # Reset the clock, or a retried document is stalled the instant it
            # is retried.
            row.status_changed_at = datetime.now(timezone.utc)
            session.commit()

        deps.runner.submit(
            process_document,
            session_factory,
            store,
            document_id,
            model_client=model_client,
            settings=settings,
        )
        return RedirectResponse("/", status_code=303)
```

with the imports at the top of the module:

```python
from datetime import datetime, timezone

from renewal.models import Document
```

- [x] **Step 4: Put the button on the stalled and failed rows**

In `renewal/templates/inbox.html`, inside the working bucket's `<li>`, after the
summary span:

```jinja
      {% if state.bucket == "stalled" %}
      <form class="inline-retry" method="post" action="/documents/{{ state.document.id }}/retry">
        <button class="btn quiet" type="submit">Try again</button>
      </form>
      {% endif %}
```

and inside the needs-you bucket's `<li>`, after the summary span:

```jinja
      {% if state.reason == "failed" %}
      <form class="inline-retry" method="post" action="/documents/{{ state.document.id }}/retry">
        <button class="btn quiet" type="submit">Try again</button>
      </form>
      {% endif %}
```

The class is `inline-retry` rather than `inline`, deliberately: `form.inline` is
bound in `app.js` to a fetch-and-reload handler built for the correction
endpoints, which answer 204. This form redirects, so it posts normally.

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_inbox.py -v`
Expected: PASS (13 tests)

- [x] **Step 6: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 762 passed

- [x] **Step 7: Commit**

```bash
git add renewal/web/inbox.py renewal/templates/inbox.html tests/test_web_inbox.py
git commit -m "feat(web): retry, the fix for the one failure mode in-process work has

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---
### Task 9: The detail view, and the one-click exception

The `needs_review` and `nothing_extracted` fixes both need the same screen: one
document, its fields, and a way to correct them. This is the stuck-documents
spec's single-document review screen, and its markup is `run_review.html`'s
field table with the run scaffolding taken off.

It also answers the question Task 8 left open: **once she has corrected a
flagged field, what files the document?** The retry button. Correcting a field
records a `Correction`, `unresolved_field_paths` then skips that path, the
promote gate passes on the next run, and the term is written. The existing
machinery already does this; it just needs a button pointed at it.

**The gate is not loosened.** `promote()` already takes `acknowledged` — the set
of flagged paths a human has looked at and accepted as they stand. That
mechanism moves here rather than being deleted with the run flow: without it a
correctly-extracted value that the extractor flagged anyway could only be
cleared by pretending to correct it, which would put a fake correction into the
dataset that exists to make extraction better.

**Files:**
- Modify: `renewal/pipeline.py` (`run_promote_stage`, `run_stages` take `acknowledged`)
- Modify: `renewal/background.py` (`process_document` takes `acknowledged`)
- Modify: `renewal/web/inbox.py` (detail route; retry accepts `acknowledged`)
- Create: `renewal/templates/inbox_detail.html`
- Test: `tests/test_web_inbox.py`, `tests/test_pipeline_promote.py`

**Interfaces:**
- Produces:
  - `run_promote_stage(session, document, *, settings, acknowledged=frozenset())`
  - `run_stages(session, store, document, *, extract_fields=True, model_client=None, settings=None, acknowledged=frozenset())`
  - `process_document(session_factory, store, document_id, *, model_client=None, settings=None, acknowledged=frozenset())`
  - `GET /documents/{document_id}/review` → `inbox_detail.html`
  - `POST /documents/{document_id}/retry` now accepts `acknowledged: list[str] = Form(default=[])`

- [x] **Step 1: Write the failing test for the gate**

Append to `tests/test_pipeline_promote.py`:

```python
def test_an_acknowledged_flag_lets_the_stage_promote(session, settings):
    """The escape hatch the run flow owned, moved rather than deleted.

    A correctly-extracted value that the extractor flagged anyway has to be
    clearable without writing a fake correction into the dataset that exists to
    make extraction better.

    The sibling above — test_a_flagged_field_waits_for_a_human — is the half of
    this that must not change: with nothing acknowledged, the stage still
    declines.
    """
    policy = _policy(session)
    document = _document(session)
    _link(session, document, client_id=policy.client_id, policy_id=policy.id)
    _extraction(session, document, needs_review=True)

    assert run_promote_stage(session, document, settings=settings) is None

    term = run_promote_stage(
        session, document, settings=settings,
        acknowledged=frozenset({"policy.total_premium"}),
    )
    assert term is not None
    assert session.query(PolicyTerm).count() == 1
```

`_policy`, `_document`, `_link` and `_extraction` are the existing helpers at
the top of this file, and `settings` is its existing fixture. Check which field
path `_extraction(..., needs_review=True)` actually flags and acknowledge that
one — it is `policy.total_premium` at the time of writing.

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_pipeline_promote.py -k acknowledged -v`
Expected: FAIL — `TypeError: run_promote_stage() got an unexpected keyword argument 'acknowledged'`

- [x] **Step 3: Thread `acknowledged` through**

In `renewal/pipeline.py`, change `run_promote_stage`'s signature and its two
uses of `unresolved_field_paths` / `promote`:

```python
def run_promote_stage(
    session: Session,
    document: Document,
    *,
    settings: Settings,
    acknowledged: frozenset[str] = frozenset(),
) -> PolicyTerm | None:
```

Replace the `unresolved_field_paths` call (currently `renewal/pipeline.py:174`):

```python
    # acknowledged is empty on every automatic run: nothing is ever waved
    # through without a human looking at it. It carries a value only when she
    # has opened the document and ticked the flagged path herself.
    if unresolved_field_paths(session, extraction.id, acknowledged):
        return None
```

and the `promote` call:

```python
        return promote(
            session, extraction, link.policy_id, kind=kind,
            acknowledged=acknowledged,
        )
```

In `run_stages`, add the parameter and pass it on:

```python
def run_stages(
    session: Session,
    store: BlobStore,
    document: Document,
    *,
    extract_fields: bool = True,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
    acknowledged: frozenset[str] = frozenset(),
) -> Document:
```

```python
    if settings is not None:
        term = run_promote_stage(
            session, document, settings=settings, acknowledged=acknowledged,
        )
```

- [x] **Step 4: Thread it through `process_document`**

In `renewal/background.py`:

```python
def process_document(
    session_factory,
    store: BlobStore,
    document_id: int,
    *,
    model_client: ModelClient | None = None,
    settings: Settings | None = None,
    acknowledged: frozenset[str] = frozenset(),
) -> None:
```

```python
            run_stages(
                session,
                store,
                document,
                model_client=model_client,
                settings=settings,
                acknowledged=acknowledged,
            )
```

- [x] **Step 5: Run the gate test**

Run: `.venv/bin/pytest tests/test_pipeline_promote.py -v`
Expected: PASS

- [x] **Step 6: Write the failing tests for the screen**

Append to `tests/test_web_inbox.py`:

```python
def test_the_detail_view_shows_the_fields_and_the_source_link(
    signed, engine, settings
):
    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )

    session = sessionmaker(bind=engine)()
    try:
        document_id = session.query(Document).one().id
    finally:
        session.close()

    with signed() as client:
        page = client.get(f"/documents/{document_id}/review")
    assert page.status_code == 200
    assert "dropped.pdf" in page.text
    # Every claim in this app is one click from the page it was read off.
    assert f'href="/documents/{document_id}"' in page.text


def test_the_detail_view_of_a_missing_document_is_a_404(signed):
    with signed() as client:
        page = client.get("/documents/999999/review")
    assert page.status_code == 404


def test_a_needs_you_row_links_to_the_detail_view(signed, engine, settings):
    _document(engine, settings, filename="mystery.pdf")
    with signed() as client:
        page = client.get("/")
    assert "/review" in page.text
```

- [x] **Step 7: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_web_inbox.py -k detail_view -v`
Expected: FAIL — 404 on `/documents/{id}/review`

- [x] **Step 8: Add the detail route**

In `renewal/web/inbox.py`:

```python
from sqlalchemy import case

from renewal.corrections import effective_values
from renewal.extract.validate import verification_rate
from renewal.inbox import state_of
from renewal.models import ExtractedField, Extraction
from renewal.promote import unresolved_field_paths

# Reading order. Alphabetical puts policy.total_premium below every coverage
# and form line, and the premium is the first thing anyone checks. Within a
# group the order stays alphabetical.
_FIELD_GROUP = case(
    (ExtractedField.field_path.like("policy.%"), 0),
    (ExtractedField.field_path.like("coverage.%"), 1),
    (ExtractedField.field_path.like("item.%"), 2),
    (ExtractedField.field_path.like("forms.%"), 3),
    else_=4,
)
```

```python
    @router.get("/documents/{document_id}/review", response_class=HTMLResponse)
    def review_document(request: Request, document_id: int):
        """One document, its fields, and its fix.

        The stuck-documents spec's single-document screen. It serves both the
        needs_review and the nothing_extracted rows, because the fix for both
        is the same table: correct what is wrong, add what is missing, then
        file it.
        """
        with session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise HTTPException(status_code=404, detail="no such document")
            state = state_of(session, document)

            extraction = (
                session.query(Extraction)
                .filter_by(document_id=document_id)
                .order_by(Extraction.id.desc())
                .first()
            )
            fields, extra, blocked, rate = [], [], [], None
            if extraction is not None:
                values = effective_values(session, extraction.id)
                fields = (
                    session.query(ExtractedField)
                    .filter_by(extraction_id=extraction.id)
                    .order_by(_FIELD_GROUP, ExtractedField.field_path)
                    .all()
                )
                for field in fields:
                    field.effective_value = values.get(
                        field.field_path, field.value
                    )
                emitted = {field.field_path for field in fields}
                extra = [
                    (path, value) for path, value in values.items()
                    if path not in emitted
                ]
                blocked = unresolved_field_paths(session, extraction.id)
                rate = verification_rate(fields)

            return TEMPLATES.TemplateResponse(
                request,
                "inbox_detail.html",
                {
                    "document": document,
                    "state": state,
                    "extraction": extraction,
                    "fields": fields,
                    "extra_fields": extra,
                    "blocked": blocked,
                    "rate": rate,
                },
            )
```

- [x] **Step 9: Let retry carry the acknowledgements**

In `renewal/web/inbox.py`, change the retry route's signature and its submit:

```python
    @router.post("/documents/{document_id}/retry")
    def retry_document(
        document_id: int, acknowledged: list[str] = Form(default=[])
    ):
```

```python
        deps.runner.submit(
            process_document,
            session_factory,
            store,
            document_id,
            model_client=model_client,
            settings=settings,
            acknowledged=frozenset(acknowledged),
        )
```

adding `Form` to the `fastapi` import line.

- [x] **Step 10: Write `renewal/templates/inbox_detail.html`**

The field table is `run_review.html`'s, unchanged: the same `input.correctable`,
`form.inline` and `form.add-missing` classes, so `app.js` drives it with no
edit at all.

```jinja
{% extends "base.html" %}
{% block title %}{{ document.original_filename }}{% endblock %}
{% block content %}
<div class="pagehead">
  <h1 class="path">{{ document.original_filename }}</h1>
  <span class="sub">{{ state.summary }}</span>
  <span class="actions">
    <a class="btn" href="/documents/{{ document.id }}">Open the PDF</a>
  </span>
</div>

<section class="panel">
  <header>
    <h2>Fields</h2>
    {% if rate is not none %}
    <span class="rate">
      {% if rate < 0.5 %}
      <span class="flag">{{ "%.1f"|format(100 * rate) }}% verified</span>
      {% else %}
      <span class="ok">{{ "%.1f"|format(100 * rate) }}% verified</span>
      {% endif %}
    </span>
    {% endif %}
  </header>

  {% if extraction %}
  <p class="meta">
    extraction #{{ extraction.id }} · {{ extraction.extractor_version }}
    <br>{{ extraction.model_id }}, status {{ extraction.status }}
  </p>
  {% endif %}

  {% if fields or extra_fields %}
  <table class="fields">
    <thead>
      <tr><th>Field</th><th>Value</th><th>Source</th></tr>
    </thead>
    <tbody>
      {% for field in fields %}
      <tr {% if field.needs_review %}class="attn"{% endif %}>
        <td class="path">{{ field.field_path | wbr }}</td>
        <td>
          <div class="value-cell">
            <input type="text" value="{{ field.effective_value or '' }}"
                   aria-label="{{ field.field_path }}"
                   data-field-id="{{ field.id }}"
                   data-original="{{ field.value or '' }}"
                   class="correctable">
            <form class="inline" method="post" action="/fields/{{ field.id }}/reject">
              <button class="btn quiet" type="submit"
                      title="This value is not on the document">not present</button>
            </form>
            <span class="saved" data-saved-for="{{ field.id }}" aria-live="polite"></span>
          </div>
        </td>
        <td class="meta">
          {% if field.needs_review %}<span class="flag">needs review</span> · {% endif %}
          confidence {{ "%.2f"|format(field.confidence) }}, page {{ field.source_page }}
          {% if field.validation_error %}
            <br><span class="flag">{{ field.validation_error }}</span>
          {% endif %}
          {% if field.source_text_span %}
            <br>{{ field.source_text_span }}
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      {% for path, value in extra_fields %}
      <tr>
        <td class="path">{{ path | wbr }}</td>
        <td class="path">{{ value }}</td>
        <td class="meta">added by hand</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">
    <p>Nothing could be read from this document.</p>
    <p>
      Usually it is a scan the text layer missed. Add the fields you need by
      hand below, or open the PDF and check it is the right file.
    </p>
  </div>
  {% endif %}

  {% if extraction %}
  <form method="post" action="/extractions/{{ extraction.id }}/fields"
        class="add-missing">
    <input type="text" name="field_path" aria-label="Field path"
           placeholder="field path, e.g. policy.total_premium" required>
    <input type="text" name="corrected_value" aria-label="Value"
           placeholder="value" required>
    <button class="btn" type="submit">Add missing field</button>
  </form>
  {% endif %}
</section>

<section class="promote">
  <h2>File it</h2>
  {% if blocked %}
  <p class="notice bad">
    These fields still need checking: {{ blocked|join(", ") }}. Correct them
    above, or tick them here to accept the extracted value as it stands.
  </p>
  {% endif %}
  <form method="post" action="/documents/{{ document.id }}/retry">
    {% if blocked %}
    <fieldset class="ack">
      {% for path in blocked %}
      <label class="inline">
        <input type="checkbox" name="acknowledged" value="{{ path }}">
        {{ path }} is correct
      </label>
      {% endfor %}
    </fieldset>
    {% endif %}
    <button class="btn primary" type="submit">File it</button>
  </form>
</section>
{% endblock %}
```

- [x] **Step 11: Link the needs-you rows to it**

In `renewal/templates/inbox.html`, in the needs-you bucket's `<li>`, add after
the summary span:

```jinja
      {% if state.reason in ("needs_review", "nothing_extracted") %}
      <a class="btn quiet" href="/documents/{{ state.document.id }}/review">Open it</a>
      {% endif %}
```

- [x] **Step 12: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_inbox.py tests/test_pipeline_promote.py -v`
Expected: PASS

- [x] **Step 13: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 766 passed

- [x] **Step 14: Commit**

```bash
git add renewal/pipeline.py renewal/background.py renewal/web/inbox.py renewal/templates/ tests/
git commit -m "feat(web): the document detail view, and the acknowledge gate moved onto it

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 10: The needs-client and needs-policy fixes, on the row

The remaining two exception states. Each carries its fix inline rather than
linking away, because the fix is one click and a screen change costs more than
the click does.

`needs_client` is today's `/unmatched`, moved: the same ranked candidates, the
same `assign` call, the same recording of what was offered against what was
chosen. `needs_policy` is new — the client is known and the policy is not, so
she picks from that client's policies, or creates one from this document.

**Files:**
- Modify: `renewal/web/unmatched.py` (drop `GET /unmatched`, redirect to `/`)
- Modify: `renewal/web/clients.py` (adopt `POST /clients`, `POST /policies`)
- Modify: `renewal/web/inbox.py` (candidates and policies on the rows)
- Modify: `renewal/templates/inbox.html`
- Modify: `renewal/templates/base.html` (drop "Unmatched")
- Delete: `renewal/templates/unmatched.html`
- Test: `tests/test_web_inbox.py`

**Interfaces:**
- Consumes: `candidates_for`, `assign` from `renewal.resolve.service`.
- Produces: `DocumentState`-keyed context `{"candidates": {document_id: [...]}, "policies": {document_id: [Policy, ...]}}`; `POST /documents/{document_id}/policy`.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_web_inbox.py`:

```python
def test_a_needs_client_row_offers_the_ranked_candidates(
    signed, engine, settings
):
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Ramirez Landscaping")
        session.add(client_row)
        session.flush()
        session.add(
            Policy(client_id=client_row.id, carrier_name="Progressive",
                   policy_number="AU-4471", line_of_business="commercial_auto")
        )
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["Policy Number: AU-4471"]]),
            original_filename="mystery.pdf", source="manual_upload",
            agency_id=1,
        )
        session.commit()
        document_id = document.id
    finally:
        session.close()

    # Text has to exist for candidate extraction to read anything.
    with signed() as client:
        client.post(f"/documents/{document_id}/retry", follow_redirects=False)
        page = client.get("/")

    assert f"/unmatched/{document_id}/assign" in page.text


def test_assigning_a_client_from_the_inbox_lands_back_on_the_inbox(
    signed, engine, settings
):
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Ramirez Landscaping")
        session.add(client_row)
        session.flush()
        client_id = client_row.id
        session.commit()
    finally:
        session.close()

    document_id = _document(engine, settings, filename="mystery.pdf")
    with signed() as client:
        response = client.post(
            f"/unmatched/{document_id}/assign",
            data={"client_id": str(client_id)},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_the_old_unmatched_page_is_gone(signed):
    with signed() as client:
        page = client.get("/unmatched", follow_redirects=False)
    assert page.status_code == 404


def test_a_needs_policy_row_offers_that_clients_policies(
    signed, engine, settings
):
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Ramirez Landscaping")
        session.add(client_row)
        session.flush()
        policy = Policy(
            client_id=client_row.id, carrier_name="Progressive",
            policy_number="AU-4471", line_of_business="commercial_auto",
        )
        session.add(policy)
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["DECLARATIONS PAGE", "premium and limits"]]),
            original_filename="dec.pdf", source="manual_upload", agency_id=1,
        )
        session.flush()
        assign(session, document.id, client_id=client_row.id, policy_id=None,
               candidates=[])
        session.commit()
        document_id, policy_id = document.id, policy.id
    finally:
        session.close()

    with signed() as client:
        response = client.post(
            f"/documents/{document_id}/policy",
            data={"policy_id": str(policy_id)},
            follow_redirects=False,
        )
    assert response.status_code == 303

    session = sessionmaker(bind=engine)()
    try:
        from renewal.resolve.service import latest_link

        assert latest_link(session, document_id).policy_id == policy_id
    finally:
        session.close()
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_web_inbox.py -k "needs_client or assigning or unmatched_page or needs_policy" -v`
Expected: FAIL — `/unmatched` still returns 200, and `/documents/{id}/policy` is a 405.

- [x] **Step 3: Strip `renewal/web/unmatched.py` to its two fixes**

Delete the `GET /unmatched` route and the `TEMPLATES`, `page_text`, `Client`
(kept — still used by the 404 check) and `SNIPPET_CHARS` imports it alone
needed. Change both redirects from `"/unmatched"` to `"/"`. Replace the module
docstring with:

```python
"""Filing a document against a client.

The queue this module used to render is now a bucket of the inbox, but the two
fixes stayed here: every assignment is a human decision recorded with the
alternatives it was chosen over, and that record is the training data for making
matching better. It is why assignment writes a row rather than setting a field.
"""
```

- [x] **Step 4: Move `POST /clients` and `POST /policies` into `renewal/web/clients.py`**

Copy the two route functions verbatim out of `renewal/web/runs.py:42-66` into
`renewal/web/clients.py`'s `register`, changing only the redirect target from
`"/runs/new"` to `"/"`, and add `Form` and `RedirectResponse` to that module's
imports. They keep their URLs; creating a policy from a document is the
`needs_policy` fix, and `POST /clients` is what the new-client form on a
`needs_client` row posts to via `/unmatched/{id}/new-client`.

- [x] **Step 5: Add `POST /documents/{document_id}/policy`**

In `renewal/web/inbox.py`:

```python
    @router.post("/documents/{document_id}/policy")
    def set_policy(document_id: int, policy_id: int = Form(...)):
        """The needs_policy fix.

        The client is already decided; this only says which of that client's
        policies the document belongs to. candidates is recomputed rather than
        taken from the form, so what she chose over is what the system actually
        offered.
        """
        with session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise HTTPException(status_code=404, detail="no such document")
            link = latest_link(session, document_id)
            if link is None:
                raise HTTPException(
                    status_code=400, detail="file this to a client first"
                )
            policy = session.get(Policy, policy_id)
            if policy is None or policy.client_id != link.client_id:
                raise HTTPException(
                    status_code=404, detail="no such policy for this client"
                )
            assign(
                session, document_id, client_id=link.client_id,
                policy_id=policy_id,
                candidates=[asdict(m) for m in candidates_for(session, document)],
            )
            session.commit()
        return RedirectResponse("/", status_code=303)
```

with the imports:

```python
from dataclasses import asdict

from renewal.models import Policy
from renewal.resolve.service import assign, candidates_for, latest_link
```

- [x] **Step 6: Put the candidates and policies on the rows**

In `renewal/web/inbox.py`'s `inbox` route, build the two lookups the needs-you
rows need — only for the rows that need them, so an inbox of Done rows costs no
extra queries:

```python
    @router.get("/", response_class=HTMLResponse)
    def inbox(request: Request):
        with session_factory() as session:
            states = inbox_rows(session)
            groups = bucketed(states)

            candidates: dict[int, list[dict]] = {}
            policies: dict[int, list[Policy]] = {}
            for state in groups["needs_you"]:
                if state.reason == "needs_client":
                    matches = candidates_for(session, state.document)
                    names = dict(
                        session.query(Client.id, Client.display_name).filter(
                            Client.id.in_([m.client_id for m in matches] or [0])
                        )
                    )
                    candidates[state.document.id] = [
                        {
                            "client_id": m.client_id,
                            "policy_id": m.policy_id,
                            "name": names.get(
                                m.client_id, f"client {m.client_id}"
                            ),
                            "score": m.score,
                            "reason": m.reason,
                        }
                        for m in matches
                    ]
                elif state.reason == "needs_policy" and state.client_id:
                    policies[state.document.id] = list(
                        session.scalars(
                            select(Policy)
                            .where(Policy.client_id == state.client_id)
                            .order_by(Policy.policy_number)
                        )
                    )

            return TEMPLATES.TemplateResponse(
                request,
                "inbox.html",
                {
                    "groups": groups,
                    "in_flight": anything_in_flight(session),
                    "candidates": candidates,
                    "policies": policies,
                },
            )
```

with `from sqlalchemy import case, select` and `from renewal.models import Client, Document, ExtractedField, Extraction, Policy`.

- [x] **Step 7: Render the two fixes**

In `renewal/templates/inbox.html`, inside the needs-you `<li>`, after the
existing "Open it" block:

```jinja
      {% if state.reason == "needs_client" %}
      <span class="fix">
        {# The score is shown on purpose: she should be able to see how close
           the near-miss was before picking the one above it. #}
        {% for candidate in candidates.get(state.document.id, []) %}
        <form class="inline-fix" method="post" action="/unmatched/{{ state.document.id }}/assign">
          <input type="hidden" name="client_id" value="{{ candidate.client_id }}">
          {% if candidate.policy_id %}
          <input type="hidden" name="policy_id" value="{{ candidate.policy_id }}">
          {% endif %}
          <button class="btn quiet" type="submit">{{ candidate.name }}</button>
          <span class="num">{{ "%.0f"|format(candidate.score * 100) }}%</span>
        </form>
        {% else %}
        <span class="muted">Nothing matched.</span>
        {% endfor %}
        <form class="inline-fix" method="post" action="/unmatched/{{ state.document.id }}/new-client">
          <input name="display_name" required aria-label="New client name"
                 placeholder="New client name">
          <button class="btn quiet" type="submit">Create</button>
        </form>
      </span>
      {% elif state.reason == "needs_policy" %}
      <span class="fix">
        <form class="inline-fix" method="post" action="/documents/{{ state.document.id }}/policy">
          <select name="policy_id" aria-label="Policy" required>
            {% for policy in policies.get(state.document.id, []) %}
            <option value="{{ policy.id }}">{{ policy.carrier_name }} {{ policy.policy_number }}</option>
            {% endfor %}
          </select>
          <button class="btn quiet" type="submit">File it here</button>
        </form>
        <a class="btn quiet" href="/clients/{{ state.client_id }}">New policy</a>
      </span>
      {% endif %}
```

and append to `renewal/static/app.css`:

```css
.fix {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem 0.7rem;
  flex: 1 1 100%;
}

.inline-fix {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
}
```

`inline-fix` rather than `inline`: `form.inline` is bound in `app.js` to the
fetch-and-reload handler for the 204 correction endpoints, and these redirect.

- [x] **Step 8: Drop "Unmatched" from the nav and delete its template**

In `renewal/templates/base.html`, delete line 35 (the `/unmatched` link).

```bash
git rm renewal/templates/unmatched.html
```

- [x] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_inbox.py -v`
Expected: PASS

- [x] **Step 10: Run the full suite**

Run: `.venv/bin/pytest`
Expected: `tests/test_web_unmatched.py` (if it exists) fails on the deleted
`GET /unmatched`. Move its assertions about the two POST fixes into
`tests/test_web_inbox.py` and delete the page-rendering ones — the page is gone,
so a test asserting it renders is testing something that no longer exists.

- [x] **Step 11: Commit**

```bash
git add -A renewal/web/ renewal/templates/ renewal/static/app.css tests/
git commit -m "feat(web): the client and policy fixes move onto the inbox rows

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---
### Task 11: The mail webhook answers first and works afterwards

`receive()` currently runs OCR and up to three model calls per attachment inside
the webhook. Real providers time out around 10-30 seconds and retry on a
non-200, so a slow extraction today means the same message is processed twice.
Moving the attachment stages to the background fixes that as a side effect of
the thing it is here to do.

The body document keeps its stages inline: they are a text insert, a regex pass
and one small classification call over text that is already in hand, with no
OCR and no field extraction. Moving them would buy nothing and would split the
message row's transaction.

**Files:**
- Modify: `renewal/mail/intake.py`
- Modify: `renewal/web/mail.py`
- Test: `tests/test_mail_intake.py`, `tests/test_web_mail.py`

**Interfaces:**
- Produces: `receive(session, store, email, *, client, settings, on_document=None) -> InboundMessage` — `on_document` is called with each attachment's `document_id` after the message row is committed.

- [x] **Step 1: Write the failing test**

Append to `tests/test_mail_intake.py`:

```python
def test_attachments_are_handed_off_rather_than_extracted_inline(
    session, store
):
    """The webhook has to answer before a provider's retry timer fires.

    A hosted provider retries any non-200, and today a slow extraction inside
    this call means the same message is processed twice.
    """
    from renewal.models import Extraction

    _agency(session)
    handed_off = []
    message = receive(
        session, store, _email(), client=StubClient('{"dates": []}'),
        settings=_settings(), on_document=handed_off.append,
    )

    assert message.processing_status == "processed"
    attachment = session.query(Document).filter_by(
        source="email_attachment"
    ).one()
    assert handed_off == [attachment.id]
    assert attachment.status == "processing"
    # The stages have not run: that is the background task's job now.
    assert session.query(Extraction).filter_by(
        document_id=attachment.id
    ).count() == 0
```

`_agency`, `_email`, `StubClient` and `_settings` are all already imported or
defined at the top of this file. `_receive` is the file's own wrapper and does
not take `on_document`, which is why this one test calls `receive` directly.

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_mail_intake.py -k handed_off -v`
Expected: FAIL — `TypeError: receive() got an unexpected keyword argument 'on_document'`

- [x] **Step 3: Change `receive` to store and hand off**

In `renewal/mail/intake.py`, change the signature:

```python
def receive(
    session: Session,
    store: BlobStore,
    email: InboundEmail,
    *,
    client: ModelClient,
    settings: Settings,
    on_document=None,
) -> InboundMessage:
```

and replace the attachment loop (currently the `ingest_document` call at
`renewal/mail/intake.py:133-138`) with:

```python
    stored: list[int] = []
    for attachment in email.attachments:
        if attachment.content_type not in PDF_TYPES:
            logger.info(
                "attachment skipped message_row_id=%s content_type=%s",
                message.id, attachment.content_type,
            )
            continue
        # Stored and recorded here; read, classified and extracted afterwards.
        # A hosted provider retries any non-200 and times out around 10-30
        # seconds, so OCR plus three model calls inside this call means the
        # same message processed twice.
        document = ingest_pdf(
            session, store, data=attachment.data,
            original_filename=attachment.filename,
            source="email_attachment", agency_id=agency.id,
            inbound_message_id=message.id,
        )
        document.status = "processing"
        session.flush()
        stored.append(document.id)
```

and after `message.processing_status = "processed"` / `session.flush()`:

```python
    if on_document is not None:
        for document_id in stored:
            on_document(document_id)
    return message
```

Change the import at the top from `ingest_document` to `ingest_pdf`:

```python
from renewal.ingest import ingest_pdf
from renewal.pipeline import (
    run_classify_stage, run_dates_stage, run_resolve_stage,
)
```

- [x] **Step 4: Dispatch from the webhook**

In `renewal/web/mail.py`, replace the `receive` call block:

```python
        pending: list[int] = []
        with session_factory() as session:
            message = receive(session, store, email,
                              client=model_client, settings=settings,
                              on_document=pending.append)
            session.commit()
            row_id, status = message.id, message.processing_status

        # After the commit: the background task opens its own session and must
        # not race a transaction that has not landed yet.
        for document_id in pending:
            deps.runner.submit(
                process_document,
                session_factory,
                store,
                document_id,
                model_client=model_client,
                settings=settings,
            )
```

with the import:

```python
from renewal.background import process_document
```

and keep a reference to `deps` in `register` (it is already the parameter name).

- [x] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mail_intake.py tests/test_web_mail.py -v`
Expected: PASS with no edits to the existing tests. None of them asserts that
`receive` produced an `Extraction` — they assert on `Document` rows, blobs,
`doc_type` and `processing_status`, all of which this change leaves alone. If
that turns out to be wrong, move the assertion behind an `InlineRunner` rather
than deleting it: the behaviour still happens, just one step later.

- [x] **Step 6: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 767 passed

- [x] **Step 7: Commit**

```bash
git add renewal/mail/intake.py renewal/web/mail.py tests/
git commit -m "fix(mail): answer the webhook before the provider's retry timer

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 12: The renewal comparison builds itself

**This is the task the whole plan exists for.**

`premium_change` — the item that says the premium moved — is raised by
`evaluate_comparison`, which runs inside `build_matrix`
(`renewal/comparison.py:245-256`). The pipeline never calls `build_matrix`; it
promotes a term and stops. So the alert that exists to prompt her to compare
only comes into existence *after she has already compared*. If she never clicks,
she is never told.

After the pipeline promotes a term, if it forms a draft-eligible pair with the
policy's previous bound term, the comparison is built and the draft generated.
This is exactly what `POST /runs/{id}/promote` does today — `build_comparison`
then `generate_draft`, both already written and already exercised. It is the
same behaviour reached without a human, not new behaviour.

**Cross-carrier comparisons are never auto-built.** `Matrix.draft_eligible` is
already exactly the right test: two `kind='bound'` terms of one policy. A quoted
column fails it, which is why setting competitors side by side still waits for a
person — that is a recommendation however it is worded.

**Files:**
- Modify: `renewal/comparison.py` (`build_comparison`'s `run_id` becomes optional)
- Modify: `renewal/pipeline.py` (`run_compare_stage`, called from `run_stages`)
- Test: `tests/test_auto_renewal.py`, `tests/test_renewal_arrives.py`

**Interfaces:**
- Consumes: `build_comparison`, `matrix_for` from `renewal.comparison`; `generate_draft` from `renewal.draft`; `load_rules` from `renewal.materiality`.
- Produces: `run_compare_stage(session, term, *, settings, model_client=None) -> Comparison | None`

- [x] **Step 1: Write the headline test**

Create `tests/test_auto_renewal.py`:

```python
"""The circularity, closed.

An emailed renewal dec page produces a premium_change attention item with no
human action at all. That is impossible before this change — the item is raised
inside build_matrix, and the pipeline never calls build_matrix — and it is the
single assertion that proves the automatic path finishes the job rather than
stopping one click short of it.
"""

from renewal.attention.rules import open_items
from renewal.comparison import matrix_for
from renewal.models import Comparison, Draft, PolicyTerm
from tests.test_renewal_arrives import Carrier, DEC_PAGE, _arrive, _incumbent


def test_a_renewal_compares_itself_and_raises_the_premium_change(
    session, store
):
    policy, prior = _incumbent(session)

    document = _arrive(session, store)

    renewal = session.query(PolicyTerm).filter_by(
        source_document_id=document.id
    ).one()
    comparison = session.query(Comparison).one()
    matrix = matrix_for(session, comparison)
    assert [c.term.id for c in matrix.columns] == [prior.id, renewal.id]

    # 3900.00 -> 4210.00 is 7.9%, under the 10% default, so this asserts the
    # rule ran rather than that it fired. renewal_received is the one that must
    # be there either way.
    reasons = {row.reason_code for row in open_items(session)}
    assert "renewal_received" in reasons


def test_the_premium_change_item_is_raised_without_a_click(session, store):
    """A move big enough to clear the threshold, reached with no human."""
    policy, prior = _incumbent(session)
    prior.total_premium = "3000.00"  # 4210.00 is +40%
    session.flush()

    _arrive(session, store)

    reasons = {row.reason_code for row in open_items(session)}
    assert "premium_change" in reasons


def test_the_draft_is_generated_too(session, store):
    _incumbent(session)
    _arrive(session, store)
    assert session.query(Draft).count() == 1


def test_a_first_term_compares_with_nothing(session, store):
    """New business has no prior term. Nothing to compare, and no error."""
    from renewal.models import Client, Policy

    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    session.add(
        Policy(client_id=client.id, carrier_name="Progressive",
               policy_number="AU-4471", line_of_business="commercial_auto",
               state="OR")
    )
    session.flush()

    _arrive(session, store)

    assert session.query(PolicyTerm).count() == 1
    assert session.query(Comparison).count() == 0


def test_the_same_renewal_twice_builds_one_comparison(session, store):
    """The twin check already stops the second promotion, so this asserts the
    compare stage cannot find a way around it."""
    from tests.pdfmaker import make_text_pdf

    _incumbent(session)
    same_file = make_text_pdf([DEC_PAGE])
    _arrive(session, store, same_file)
    _arrive(session, store, same_file)

    assert session.query(Comparison).count() == 1
    assert session.query(Draft).count() == 1


def test_a_quote_is_never_auto_compared(session, store):
    """Setting competitors side by side is a recommendation however it is
    worded. It waits for a person, and this is the assertion that keeps it
    waiting."""
    import json

    from renewal.config import Settings
    from tests.test_dates_llm import _settings

    policy, prior = _incumbent(session)

    class Quoting(Carrier):
        def complete(self, *, model, system, content):
            settings = _settings()
            if model == settings.classification_model:
                return json.dumps({"doc_class": "quote", "confidence": 0.95})
            return super().complete(model=model, system=system, content=content)

    from renewal.pipeline import ingest_document
    from tests.pdfmaker import make_text_pdf

    ingest_document(
        session, store, data=make_text_pdf([DEC_PAGE]),
        original_filename="quote.pdf", source="email_attachment", agency_id=1,
        model_client=Quoting(), settings=_settings(),
    )

    quoted = session.query(PolicyTerm).filter_by(kind="quoted").one()
    assert quoted.policy_id == policy.id
    assert session.query(Comparison).count() == 0
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_auto_renewal.py -v`
Expected: FAIL — `assert 0 == 1`, no `Comparison` rows. This is the circularity,
reproduced.

- [x] **Step 3: Let `build_comparison` be built without a run**

In `renewal/comparison.py`, change `build_comparison`'s signature — `run_id`
becomes optional, matching `build_matrix`, which has always accepted `None`:

```python
def build_comparison(
    session: Session,
    *,
    prior_term: PolicyTerm,
    renewal_term: PolicyTerm,
    rules: RuleSet,
    run_id: int | None = None,
    settings: Settings | None = None,
) -> Comparison:
    """The pairwise case.

    run_id is optional and is never passed any more: RenewalRun is not written
    by anything after the automatic-intake change. It stays in the signature
    because comparisons built before that change carry one, and it is the only
    record of what those rows meant.
    """
```

Note `run_id` moves after `rules` so the keyword-only call sites are unaffected.

- [x] **Step 4: Add the compare stage**

In `renewal/pipeline.py`:

```python
def _already_compared(session: Session, baseline_id: int, comparand_id: int) -> bool:
    """Both terms already sit in one comparison together."""
    mine = select(ComparisonColumn.comparison_id).where(
        ComparisonColumn.policy_term_id == comparand_id
    )
    return session.scalar(
        select(ComparisonColumn.comparison_id)
        .where(ComparisonColumn.policy_term_id == baseline_id)
        .where(ComparisonColumn.comparison_id.in_(mine))
        .limit(1)
    ) is not None


def run_compare_stage(
    session: Session,
    term: PolicyTerm,
    *,
    settings: Settings,
    model_client: ModelClient | None = None,
) -> Comparison | None:
    """Build the renewal comparison the moment the renewal term exists.

    Without this the pipeline promotes a term and stops, and premium_change --
    the item whose whole job is to prompt her to compare -- is raised inside
    build_matrix, which only runs once she already has. The alert lived behind
    the click it existed to prompt.

    Renewals only. A quoted term is not draft_eligible and never reaches the
    build: putting competitors side by side is a recommendation however it is
    worded, and that still waits for a person.
    """
    if term.kind != "bound":
        return None
    prior = session.scalar(
        select(PolicyTerm)
        .where(PolicyTerm.policy_id == term.policy_id)
        .where(PolicyTerm.kind == "bound")
        .where(PolicyTerm.id != term.id)
        .order_by(
            PolicyTerm.effective_date.desc().nullslast(), PolicyTerm.id.desc()
        )
        .limit(1)
    )
    if prior is None:
        return None  # new business: nothing to compare against
    if _already_compared(session, prior.id, term.id):
        return None
    try:
        comparison = build_comparison(
            session,
            prior_term=prior,
            renewal_term=term,
            rules=load_rules(settings.materiality_config),
            settings=settings,
        )
        matrix = matrix_for(session, comparison, settings=settings)
        if matrix.draft_eligible and model_client is not None:
            generate_draft(
                session, matrix, client=model_client, settings=settings
            )
        return comparison
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("compare stage failed policy_term_id=%s", term.id)
        return None
```

with the imports at the top of `renewal/pipeline.py`:

```python
from renewal.comparison import build_comparison, matrix_for
from renewal.draft import generate_draft
from renewal.materiality import load_rules
from renewal.models import (
    Comparison, ComparisonColumn, Document, DocumentText, Extraction, PolicyTerm,
)
```

- [x] **Step 5: Call it from `run_stages`**

In `renewal/pipeline.py`'s `run_stages`, change the promote block:

```python
    # After the fields stage, because it promotes what that stage extracted,
    # and before attention, because Task 9's rules read the term it writes.
    if settings is not None:
        term = run_promote_stage(
            session, document, settings=settings, acknowledged=acknowledged,
        )
        if term is not None:
            evaluate_promotion(session, term)
            # The renewal comparison, built without being asked. This is what
            # puts premium_change in front of her instead of behind a click she
            # has to know to make.
            run_compare_stage(
                session, term, settings=settings, model_client=model_client,
            )
```

- [x] **Step 6: Check for an import cycle**

`renewal/pipeline.py` now imports `renewal.comparison`, which imports
`renewal.attention.rules`. Nothing in either may import `renewal.pipeline`.

Run: `.venv/bin/python -c "import renewal.pipeline, renewal.comparison, renewal.web; print('ok')"`
Expected: `ok`

If it fails, the cycle is `renewal.inbox` importing `should_extract_fields` from
`renewal.pipeline` — move that one function into `renewal/classify/runner.py`
beside `FIELD_EXTRACTION_CLASSES`, where it always belonged, and import it from
there in both places.

- [x] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_auto_renewal.py -v`
Expected: PASS (6 tests)

- [x] **Step 8: Fix the test this deliberately invalidates**

`tests/test_renewal_arrives.py::test_nothing_is_compared_until_she_clicks`
asserts the old behaviour and is now wrong on purpose. Replace it:

```python
def test_a_renewal_compares_itself(session, store):
    """This assertion used to read the other way.

    "Nothing is compared until she clicks" was the design until the click was
    found to be sitting in front of the notification that existed to prompt it.
    A renewal is one policy's own history and the arithmetic is the same
    whoever asks for it, so it is built when the term lands. A quote still
    waits: see tests/test_auto_renewal.py.
    """
    _incumbent(session)
    _arrive(session, store)
    assert session.query(Comparison).count() == 1
    assert session.query(Draft).count() == 1
```

- [x] **Step 9: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 773 passed

- [x] **Step 10: Commit**

```bash
git add renewal/pipeline.py renewal/comparison.py tests/
git commit -m "feat(pipeline): build the renewal comparison when the term lands

premium_change was raised inside build_matrix, which the pipeline never
called, so the alert that exists to prompt her to compare only appeared
after she had already compared. Closes that.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 13: The badge and the email

A count beside the inbox link, always. Plus an email when documents are waiting,
sent by the background task that found them — so no scheduler is required, which
matters because `renewal/attention/rules.py:13` records that materialising a
time-based reason "would need a scheduler this design does not have".

**This is the first outbound mail the application sends.** The principle it
appears to break — that nothing is sent from here — is about client-facing mail
and remains intact: no draft, no comparison and no client communication is ever
sent automatically. The only recipient is the operator.

**The guard, stated precisely** (the spec's two sentences under-determine it):
an email goes out when the needs-you count is above zero and the most recent
send is either absent or older than `notify_min_interval_minutes`. It lists
everything currently waiting, not the one document that triggered it. A bulk
import that strands twenty documents therefore sends one email, not twenty.

**Files:**
- Modify: `renewal/models.py` (`NotificationSend`)
- Create: `migrations/versions/<rev>_notification_send.py`
- Modify: `renewal/config.py`
- Create: `renewal/notify.py`
- Create: `renewal/web/navbadge.py`
- Modify: `renewal/web/templating.py`, `renewal/templates/base.html`
- Modify: `renewal/background.py` (notify at the end of a run)
- Modify: `renewal/web/__init__.py` (install the middleware)
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces:
  - `NotificationSend(id, sent_at, document_count)`
  - `maybe_notify(session, *, settings, send=None) -> bool`
  - `send_email(subject, body, *, settings) -> None`
  - `renewal.web.navbadge.install(app, deps)`

- [x] **Step 1: Add the model and its migration**

In `renewal/models.py`:

```python
class NotificationSend(Base):
    """One row per operator email sent.

    A row rather than a column on agency, because the count at the time is
    worth keeping: it is the only record of how big the backlog got, and the
    guard reads the timestamp off the newest row anyway.
    """

    __tablename__ = "notification_send"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_count: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = _created_at()
```

Create `migrations/versions/e70c8b2a5941_notification_send.py`:

```python
"""notification send

Revision ID: e70c8b2a5941
Revises: c4a1f0b83d17
Create Date: 2026-09-14 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e70c8b2a5941'
down_revision: Union[str, Sequence[str], None] = 'c4a1f0b83d17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_send",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("notification_send")
```

Add `notification_send` to the `TABLES` tuple in `conftest.py:48-56` so the
`clean_db` fixture truncates it.

- [x] **Step 2: Add the settings**

In `renewal/config.py`, add to the `Settings` dataclass:

```python
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    notify_from: str = ""
    notify_to: str = ""
    notify_min_interval_minutes: int = 60
    base_url: str = "http://127.0.0.1:8000"
```

and to `load_settings()`:

```python
        # Empty host means notifications are off, which is the default. A
        # misconfigured mail server must never be able to cost a document.
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=os.environ.get("SMTP_USERNAME", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        notify_from=os.environ.get("NOTIFY_FROM", ""),
        notify_to=os.environ.get("NOTIFY_TO", ""),
        notify_min_interval_minutes=int(
            os.environ.get("NOTIFY_MIN_INTERVAL_MINUTES", "60")
        ),
        base_url=os.environ.get("BASE_URL", "http://127.0.0.1:8000"),
```

and append to `.env.example` (create the block if the file has no mail section):

```
# Operator notifications. Leave SMTP_HOST empty to turn them off.
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
NOTIFY_FROM=
NOTIFY_TO=
NOTIFY_MIN_INTERVAL_MINUTES=60
BASE_URL=http://127.0.0.1:8000
```

- [x] **Step 3: Write the failing tests**

Create `tests/test_notify.py`:

```python
"""Telling her something is waiting, at most once an hour.

The first outbound mail this application sends. It goes to the operator and
says how many documents need her — never to a client, and never carrying a
draft, a comparison, or anything about a policy.
"""

from datetime import datetime, timedelta, timezone

from renewal.ingest import ingest_pdf
from renewal.models import NotificationSend
from renewal.notify import maybe_notify
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import _settings


def _settings_with_mail(**overrides):
    base = _settings()
    return type(base)(
        **{
            **base.__dict__,
            "smtp_host": "smtp.example.com",
            "notify_to": "her@agency.com",
            "notify_from": "app@agency.com",
            **overrides,
        }
    )


def _waiting(session, store, n=1):
    for i in range(n):
        ingest_pdf(
            session, store, data=make_text_pdf([[f"unrecognisable {i}"]]),
            original_filename=f"{i}.pdf", source="manual_upload", agency_id=1,
        )
    session.flush()


def test_nothing_waiting_sends_nothing(session, store):
    sent = []
    assert not maybe_notify(
        session, settings=_settings_with_mail(), send=lambda *a, **k: sent.append(a)
    )
    assert sent == []


def test_something_waiting_sends_once(session, store):
    _waiting(session, store, 2)
    sent = []
    assert maybe_notify(
        session, settings=_settings_with_mail(),
        send=lambda subject, body, **k: sent.append((subject, body)),
    )
    assert len(sent) == 1
    assert "2" in sent[0][0]
    assert session.query(NotificationSend).one().document_count == 2


def test_a_second_call_inside_the_hour_sends_nothing(session, store):
    """A bulk import that strands twenty documents sends one email, not
    twenty."""
    _waiting(session, store, 2)
    sent = []
    send = lambda subject, body, **k: sent.append(subject)  # noqa: E731

    maybe_notify(session, settings=_settings_with_mail(), send=send)
    _waiting(session, store, 3)
    maybe_notify(session, settings=_settings_with_mail(), send=send)

    assert len(sent) == 1


def test_an_hour_later_it_sends_again(session, store):
    _waiting(session, store, 1)
    sent = []
    send = lambda subject, body, **k: sent.append(subject)  # noqa: E731

    maybe_notify(session, settings=_settings_with_mail(), send=send)
    row = session.query(NotificationSend).one()
    row.sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.flush()

    maybe_notify(session, settings=_settings_with_mail(), send=send)
    assert len(sent) == 2


def test_no_smtp_host_means_notifications_are_off(session, store):
    _waiting(session, store, 1)
    sent = []
    assert not maybe_notify(
        session, settings=_settings_with_mail(smtp_host=""),
        send=lambda *a, **k: sent.append(a),
    )
    assert sent == []
    assert session.query(NotificationSend).count() == 0


def test_an_smtp_failure_is_swallowed_and_logged(session, store):
    """A notification that cannot be sent must not cost the document."""
    _waiting(session, store, 1)

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    assert not maybe_notify(
        session, settings=_settings_with_mail(), send=boom
    )
    # No row: nothing was sent, so nothing may claim it was. The next call
    # tries again rather than being locked out by a failure.
    assert session.query(NotificationSend).count() == 0
```

- [x] **Step 4: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.notify'`

- [x] **Step 5: Write `renewal/notify.py`**

```python
"""Telling the operator that something is waiting.

The first outbound mail this application sends, and the only one it will send.
The principle it appears to break -- that nothing is sent from here -- is about
client-facing mail and is intact: no draft, no comparison and no client
communication is ever sent automatically. This says a number and a link.

No scheduler. The background task that produced the backlog is what notices it,
which is why attention/rules.py:13 can go on being true.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.inbox import needs_you_count
from renewal.models import NotificationSend

logger = logging.getLogger(__name__)


def send_email(subject: str, body: str, *, settings: Settings) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.notify_from
    message["To"] = settings.notify_to
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def _last_send(session: Session) -> NotificationSend | None:
    return session.scalar(
        select(NotificationSend).order_by(NotificationSend.id.desc()).limit(1)
    )


def maybe_notify(session: Session, *, settings: Settings, send=None) -> bool:
    """Send one email if anything is waiting and nothing was sent recently.

    Returns whether an email went out. The count is computed the same way the
    page computes it, so the email and the badge cannot disagree.
    """
    if not settings.smtp_host or not settings.notify_to:
        return False  # off, which is the default

    count = needs_you_count(session)
    if count == 0:
        return False

    last = _last_send(session)
    if last is not None:
        sent_at = last.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        window = timedelta(minutes=settings.notify_min_interval_minutes)
        if datetime.now(timezone.utc) - sent_at < window:
            return False

    subject = (
        f"{count} document{'' if count == 1 else 's'} need"
        f"{'s' if count == 1 else ''} you"
    )
    body = (
        f"{count} document{'' if count == 1 else 's'} in the inbox could not be "
        "filed without a person.\n\n"
        f"{settings.base_url}/\n"
    )

    try:
        (send or send_email)(subject, body, settings=settings)
    except Exception:  # noqa: BLE001 - a notification must not cost a document
        logger.exception("notification send failed count=%s", count)
        # Deliberately no row: nothing was sent, so nothing may claim it was,
        # and the next run tries again instead of being locked out by a
        # failure it had no part in.
        return False

    session.add(NotificationSend(document_count=count))
    session.flush()
    logger.info("notification sent count=%s", count)
    return True
```

- [x] **Step 6: Call it at the end of a background run**

In `renewal/background.py`, inside `process_document`, after the successful
`_set_status(session, document, "processed")`:

```python
            if settings is not None:
                try:
                    maybe_notify(session, settings=settings)
                    session.commit()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "notify failed document_id=%s", document_id
                    )
                    session.rollback()
```

with `from renewal.notify import maybe_notify`.

- [x] **Step 7: Run the notify tests**

Run: `.venv/bin/pytest tests/test_notify.py tests/test_background.py -v`
Expected: PASS

- [x] **Step 8: Add the nav badge**

Create `renewal/web/navbadge.py`:

```python
"""The count beside the inbox link.

A middleware rather than a per-route context entry: the nav is on every page,
and thirteen routes each remembering to pass the same number is twelve chances
to forget.
"""

from __future__ import annotations

import logging
import re

from renewal.inbox import needs_you_count
from renewal.web.deps import Deps

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD"})

# The PDF route, and only it. /documents/{id}/review is an HTML page and needs
# the badge like every other page; matching the prefix would silently strip it
# from the one screen she reaches most often from a needs-you row.
_PDF_ROUTE = re.compile(r"^/documents/\d+$")


def _renders_nav(request) -> bool:
    path = request.url.path
    if request.method not in SAFE_METHODS:
        return False
    # A static file, a PDF download and a 204 from a correction endpoint all
    # render no nav, so the query would be waste.
    return not path.startswith("/static/") and not _PDF_ROUTE.match(path)


def install(app, deps: Deps) -> None:
    session_factory = deps.session_factory

    @app.middleware("http")
    async def badge(request, call_next):
        if _renders_nav(request):
            try:
                with session_factory() as session:
                    request.state.needs_you_count = needs_you_count(session)
            except Exception:  # noqa: BLE001 - a badge must never 500 a page
                logger.exception("badge count failed")
        return await call_next(request)
```

In `renewal/web/templating.py`, add the context processor:

```python
def _nav(request) -> dict:
    """Defaults to zero so a template rendered outside a request cycle — an
    error page, a test — still renders."""
    return {"needs_you_count": getattr(request.state, "needs_you_count", 0)}


TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates"),
    context_processors=[_nav],
)
```

replacing the existing `TEMPLATES = Jinja2Templates(...)` line, and moving
`_nav` above it.

In `renewal/web/__init__.py`, install it alongside the gate:

```python
    security.install(app, deps)
    navbadge.install(app, deps)
```

with `navbadge` added to the `from renewal.web import security` line:

```python
from renewal.web import navbadge, security
```

In `renewal/templates/base.html`, change the inbox nav link to carry the count:

```jinja
      <a href="/" {% if path == "/" %}aria-current="page"{% endif %}>Inbox{% if needs_you_count %} <span class="badge">{{ needs_you_count }}</span>{% endif %}</a>
```

and append to `renewal/static/app.css`:

```css
.badge {
  display: inline-block;
  min-width: 1.15rem;
  padding: 0 0.3rem;
  border-radius: 0.6rem;
  background: var(--warn);
  color: var(--bg);
  font-size: 0.78rem;
  font-weight: 600;
  text-align: center;
}
```

- [x] **Step 9: Test the badge**

Append to `tests/test_web_inbox.py`:

```python
def test_the_nav_carries_the_needs_you_count(signed, engine, settings):
    _document(engine, settings, filename="a.pdf")
    _document(engine, settings, filename="b.pdf")
    with signed() as client:
        page = client.get("/calendar")
    assert 'class="badge">2<' in page.text


def test_an_empty_queue_shows_no_badge(signed):
    with signed() as client:
        page = client.get("/calendar")
    assert 'class="badge"' not in page.text
```

- [x] **Step 10: Run the full suite**

Run: `.venv/bin/pytest`
Expected: 783 passed

- [x] **Step 11: Commit**

```bash
git add -A renewal/ migrations/ conftest.py tests/ .env.example
git commit -m "feat(notify): a badge on every page and one operator email an hour

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

### Task 14: Delete the run flow

Everything that replaces it now exists. `POST /runs` never called
`ingest_document` — it called `ingest_pdf` and `extract` directly
(`renewal/web/runs.py:90`, `:93`, `:104`), skipping resolution, dates,
classification, promotion and attention. It needed a human to pick the policy
and press promote *because it never ran the stages that would have done those
things*. This deletion is a gain in capability, not a trade.

`RenewalRun` stays as a table and is never written again.

**Files:**
- Delete: `renewal/web/runs.py`, `renewal/templates/run_new.html`, `renewal/templates/run_review.html`, `tests/test_web_review.py`
- Rename: `renewal/web/review.py` → `renewal/web/corrections.py`
- Modify: `renewal/web/__init__.py`, `renewal/templates/comparison.html`
- Test: `tests/test_web_inbox.py`

**Interfaces:**
- The three correction endpoints keep their URLs exactly: `POST /fields/{id}/correct`, `POST /fields/{id}/reject`, `POST /extractions/{id}/fields`. `app.js` posts to them by literal path and must not need an edit.

- [x] **Step 1: Write the failing test**

Append to `tests/test_web_inbox.py`:

```python
@pytest.mark.parametrize("path", ["/runs/new", "/runs/1/review"])
def test_the_run_pages_are_gone(signed, path):
    with signed() as client:
        page = client.get(path, follow_redirects=False)
    assert page.status_code == 404


@pytest.mark.parametrize("path", ["/runs", "/runs/1/promote"])
def test_the_run_posts_are_gone(signed, path):
    with signed() as client:
        page = client.post(path, follow_redirects=False)
    assert page.status_code in (404, 405)


def test_the_correction_endpoints_kept_their_urls(signed, engine, settings):
    """app.js posts to these by literal path. Moving them would break every
    correction silently, with a 404 nobody sees."""
    from renewal.models import Correction, Extraction

    with signed() as client:
        client.post(
            "/documents",
            files={"document": ("dropped.pdf",
                                make_text_pdf([["Policy Number: AU-4471"]]),
                                "application/pdf")},
            follow_redirects=False,
        )

    session = sessionmaker(bind=engine)()
    try:
        extraction = session.query(Extraction).first()
    finally:
        session.close()
    if extraction is None:
        pytest.skip("no model client configured in this fixture")

    with signed() as client:
        response = client.post(
            f"/extractions/{extraction.id}/fields",
            data={"field_path": "policy.total_premium",
                  "corrected_value": "1234.00"},
        )
    assert response.status_code == 204
```

- [x] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_web_inbox.py -k "run_pages or run_posts" -v`
Expected: FAIL — `/runs/new` returns 200

- [x] **Step 3: Rename the review module and strip it**

```bash
git mv renewal/web/review.py renewal/web/corrections.py
```

In `renewal/web/corrections.py`, delete the `review` route (lines 56-102) and
the `promote_run` route (lines 156-201), and the `_latest_extraction` helper,
which only they used. Delete every import only they needed:
`RedirectResponse`, `HTMLResponse`, `Request`, `Session`, `evaluate_promotion`,
`build_comparison`, `matrix_for`, `generate_draft`, `verification_rate`,
`load_rules`, `Client`, `Policy`, `RenewalRun`, `PromotionBlocked`, `promote`,
`unresolved_field_paths`, `effective_values`, `TEMPLATES`, and the `_FIELD_GROUP`
`case` expression (it moved to `renewal/web/inbox.py` in Task 9).

What is left is the three correction endpoints and this docstring:

```python
"""Correcting one extracted field.

Three endpoints, posted to by app.js from the document detail view. They keep
the URLs they had under the run flow because app.js posts to them by literal
path: moving them would break every correction silently, with a 404 nobody
sees.

Every correction is a row rather than an edit. The corrections table is the
long-term asset — it is the training data for making extraction better — which
is why a value reverted to what the extractor said is still recorded.
"""
```

The remaining imports are:

```python
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import Response

from renewal.corrections import record_correction
from renewal.models import ExtractedField
from renewal.web.deps import Deps
```

and `register` keeps only `session_factory = deps.session_factory`.

- [x] **Step 4: Delete the run module and its templates**

```bash
git rm renewal/web/runs.py renewal/templates/run_new.html renewal/templates/run_review.html tests/test_web_review.py
```

`tests/test_web_review.py` tests the two-upload form and the promote screen,
both of which are gone. Before deleting it, read it once and confirm every
assertion it makes about the *correction endpoints* has an equivalent in
`tests/test_web_inbox.py`; move over anything that does not.

- [x] **Step 5: Unmount them**

In `renewal/web/__init__.py`, update the import and `ROUTER_MODULES`:

```python
from renewal.web import (
    attention as attention_routes, auth as auth_routes, calendar,
    clients as client_routes, comparison, corrections,
    inbox as inbox_routes, mail as mail_routes, search as search_routes,
    settings as settings_routes, unmatched,
)

ROUTER_MODULES = (
    inbox_routes, corrections, comparison, unmatched, calendar,
    settings_routes, search_routes, client_routes, attention_routes,
    auth_routes,
)
```

- [x] **Step 6: Drop the run crumb from the comparison page**

In `renewal/templates/comparison.html`, find the conditional block that links
back to `/runs/{{ ... }}/review` and delete it. Leave any other use of
`matrix.comparison.renewal_run_id` alone if it only *displays* a number — the
column is still the only record of what pre-matrix comparisons meant.

- [x] **Step 7: Run the full suite**

Run: `.venv/bin/pytest`
Expected: PASS. `tests/test_web_gate.py` lists `/runs/new` among the paths it
checks the gate on — replace that entry with `/documents/1/review`, which is the
protected route that took its place, rather than dropping the case.

- [x] **Step 8: Check nothing still points at a deleted route**

```bash
grep -rn "runs/new\|/runs/\|run_review\|run_new\|RenewalRun" \
  renewal/ tests/ --include="*.py" --include="*.html" --include="*.js"
```

Expected: only `renewal/models.py` (the table), `renewal/comparison.py`
(`renewal_run_id` on the `Comparison` row), and the migration that created it.
Anything else is a dangling reference.

- [x] **Step 9: Run the app and drive it**

```bash
.venv/bin/uvicorn renewal.web:app --host 127.0.0.1 --port 8000
```

Then, signed in at `http://127.0.0.1:8000/`:
1. Drop a PDF in the box. The row appears immediately as "Reading this now".
2. Wait; the page refreshes itself and the row moves to Done or Needs you.
3. Check the nav badge matches the Needs you count.
4. Open a Done row's comparison link.
5. Confirm `/runs/new` is a 404 page, not a stack trace.

- [x] **Step 10: Commit**

```bash
git add -A
git commit -m "feat(web): delete the renewal run flow

POST /runs called ingest_pdf and extract directly, never ingest_document, so
it skipped linking, dates, classification, promotion and attention. It needed
a human to pick the policy and press promote because it never ran the stages
that would have done those things. RenewalRun stays as a table and is never
written again.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013jrgvohFTj9YuD916qDri9"
```

---

## Verification against the spec

Run these after Task 14. Each maps to a line in the spec's Testing section.

- [x] **Each bucket renders, with the right fix on each needs-you row** — `.venv/bin/pytest tests/test_web_inbox.py tests/test_inbox.py -v`
- [x] **A background task flips a document from processing to its final status** — `.venv/bin/pytest tests/test_background.py -v`
- [x] **A stalled document is listed and recovers on retry** — `tests/test_web_inbox.py::test_retry_reruns_a_stalled_document`
- [x] **Auto-build fires for a renewal pair, never for a quoted column** — `tests/test_auto_renewal.py::test_a_quote_is_never_auto_compared`
- [x] **The deleted routes 404** — `tests/test_web_inbox.py::test_the_run_pages_are_gone`
- [x] **The notification sends once inside the hour, and an SMTP failure leaves the document processed** — `.venv/bin/pytest tests/test_notify.py -v`
- [x] **The regression that matters most** — `tests/test_auto_renewal.py::test_the_premium_change_item_is_raised_without_a_click`
- [x] **Full suite green** — `.venv/bin/pytest`

Then use `superpowers:finishing-a-development-branch`.
