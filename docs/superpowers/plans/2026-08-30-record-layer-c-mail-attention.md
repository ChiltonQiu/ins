# Record Layer, Plan C: Inbound Mail, Classification, Attention — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carrier mail forwarded to a unique intake address enters the record automatically, every document gets a coarse label, and anything that looks like it needs a human response shows up in one list she clears by hand.

**Architecture:** Mail arrives at a webhook behind a thin `InboundProvider` interface with a local file-drop implementation for development. The message body becomes a document alongside its attachments, so body prose flows through the same text store and the same date extraction. Classification is a cheap LLM call over page-1 text that routes and labels but gates nothing. Attention items are rows for event-triggered reasons and a live query for the one that changes with the clock.

**Tech Stack:** FastAPI, Python `email` from the standard library, SQLAlchemy 2.0, Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-record-layer-design.md`

**Covers:** spec build-order steps 8–9. Requires Plans A and B complete.

## Global Constraints

- Every table is insert-only. No UPDATE, no DELETE in application code.
- Logging carries ids, hashes, counts, and statuses only. Never message bodies, never document content, never sender addresses beyond what the row already stores.
- **Nothing auto-resolves and nothing auto-acts.** We cannot detect that she replied without reading sent mail, which needs OAuth this project deliberately does not do.
- No outbound email or messaging of any kind, to anyone. No automated follow-up or reminder sequences. Reminders surface in her queue as drafts she sends herself.
- Classification never gates storage, text extraction, search, or date extraction. `unknown` is always acceptable and preferred to a confident wrong guess.
- Attention rules bias toward over-flagging. Missing an item is the dangerous failure; dismissal is one keystroke.
- One concern per commit. All schema for this plan already landed in Plan A — this plan adds no migrations.

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `renewal/classify/__init__.py`, `prompt_v1.py`, `runner.py` | The coarse label |
| `renewal/mail/__init__.py`, `provider.py` | The `InboundProvider` interface and its payload shape |
| `renewal/mail/filedrop.py` | Local `.eml` implementation for development and tests |
| `renewal/mail/parse.py` | MIME to body text and attachments |
| `renewal/mail/intake.py` | Routing, quarantine, dedupe, document creation |
| `renewal/web/mail.py` | The webhook route |
| `renewal/attention/__init__.py`, `rules.py` | Event-triggered items and the live rule |
| `renewal/web/attention.py`, `renewal/templates/attention.html` | The queue |

**Modified**

| Path | Change |
|---|---|
| `renewal/pipeline.py` | Classification and attention stages |
| `renewal/web/__init__.py` | Register the two new routers |
| `renewal/templates/client.html` | Attention and messages sections now have data |
| `evals/test_dates.py` | Classification scoring joins the run |

---

## Task 1: Classification

A coarse, low-stakes label. It decides routing and display and nothing else.

**Files:**
- Create: `renewal/classify/__init__.py`, `renewal/classify/prompt_v1.py`, `renewal/classify/runner.py`
- Test: `tests/test_classify.py` (create)

**Interfaces:**
- Consumes: `ModelClient`, `Settings`, `page_text`, model `DocumentClassification`.
- Produces: `DOC_CLASSES: tuple[str, ...]`, `CLASSIFIER_VERSION = "classify-v1"`, `FIELD_EXTRACTION_CLASSES = ("declarations", "endorsement")`, `parse_classification(raw: str) -> tuple[str, float]`, `classify(session, document, *, client, settings) -> DocumentClassification`, `latest_class(session, document_id) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classify.py
import json

import pytest

from renewal.classify.runner import (
    CLASSIFIER_VERSION, DOC_CLASSES, classify, latest_class, parse_classification,
)
from renewal.models import DocumentClassification
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings


def _document(session, store, lines):
    from renewal.pipeline import ingest_document
    return ingest_document(
        session, store, data=make_text_pdf([lines]), original_filename="d.pdf",
        source="bulk_import", agency_id=1,
        model_client=StubClient('{"dates": []}'), settings=_settings(),
    )


def test_the_class_list_matches_the_spec():
    assert DOC_CLASSES == (
        "declarations", "endorsement", "cancellation_notice",
        "non_renewal_notice", "invoice", "id_card", "loss_run",
        "inspection_report", "quote", "correspondence", "unknown",
    )


def test_parses_a_label_and_confidence():
    assert parse_classification(
        json.dumps({"doc_class": "invoice", "confidence": 0.8})
    ) == ("invoice", 0.8)


def test_an_unrecognised_label_becomes_unknown_rather_than_being_stored():
    """A label outside the set is a model error, and unknown is the honest
    result of a model error."""
    assert parse_classification(
        json.dumps({"doc_class": "renewal_thingy", "confidence": 0.9})
    ) == ("unknown", 0.0)


def test_unparseable_output_becomes_unknown_not_an_exception():
    """Classification must never fail an ingest; it gates nothing."""
    assert parse_classification("I'm not sure what that is.") == ("unknown", 0.0)


def test_classification_is_stored_with_its_version_and_model(session, store):
    document = _document(session, store, ["NOTICE OF CANCELLATION"])
    stub = StubClient(json.dumps({"doc_class": "cancellation_notice",
                                  "confidence": 0.9}))
    row = classify(session, document, client=stub, settings=_settings())
    assert row.doc_class == "cancellation_notice"
    assert row.classifier_version == CLASSIFIER_VERSION
    assert "classification-model" in row.model_id


def test_only_page_one_text_is_sent(session, store):
    document = _document(session, store, ["page one only"])
    stub = StubClient(json.dumps({"doc_class": "unknown", "confidence": 0.1}))
    classify(session, document, client=stub, settings=_settings())
    assert "page one only" in stub.calls[0][2][0]["text"]


def test_the_classification_model_is_used(session, store):
    document = _document(session, store, ["x"])
    stub = StubClient(json.dumps({"doc_class": "unknown", "confidence": 0.1}))
    classify(session, document, client=stub, settings=_settings())
    assert stub.calls[0][0] == "classification-model"


def test_re_classification_appends_and_the_latest_wins(session, store):
    document = _document(session, store, ["NOTICE OF CANCELLATION"])
    classify(session, document,
             client=StubClient(json.dumps({"doc_class": "unknown",
                                           "confidence": 0.1})),
             settings=_settings())
    classify(session, document,
             client=StubClient(json.dumps({"doc_class": "cancellation_notice",
                                           "confidence": 0.9})),
             settings=_settings())
    assert session.query(DocumentClassification).filter_by(
        document_id=document.id).count() == 2
    assert latest_class(session, document.id) == "cancellation_notice"


def test_a_model_failure_records_unknown_rather_than_nothing(session, store):
    """An absent row and a declined answer are different facts."""
    class Exploding:
        def complete(self, **kwargs):
            raise RuntimeError("provider down")

    document = _document(session, store, ["x"])
    row = classify(session, document, client=Exploding(), settings=_settings())
    assert row.doc_class == "unknown"


def test_a_document_with_no_text_is_unknown_without_a_model_call(session, store):
    document = _document(session, store, [])
    stub = StubClient(json.dumps({"doc_class": "invoice", "confidence": 0.9}))
    row = classify(session, document, client=stub, settings=_settings())
    assert row.doc_class == "unknown"
    assert stub.calls == []
```

Extend `_settings()` in `tests/test_dates_llm.py` to include
`classification_model="classification-model"`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_classify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.classify'`

- [ ] **Step 3: Write the prompt**

```python
# renewal/classify/prompt_v1.py
"""The document classification prompt.

Coarse and low-stakes on purpose. The label decides routing and display; it
never decides whether a document is stored, searchable, or date-extracted.
That separation is why "unknown" costs almost nothing and a confident wrong
guess costs more than it looks like it should.
"""

VERSION = "classify-v1"

SYSTEM = """\
You label insurance documents by type from the first page.

Answer "unknown" whenever you are not confident. An unknown label is a correct
and useful answer; a confident wrong label is worse than no label at all,
because it sends the document down the wrong path silently.

Choose exactly one of: declarations, endorsement, cancellation_notice,
non_renewal_notice, invoice, id_card, loss_run, inspection_report, quote,
correspondence, unknown.

Answer with JSON only: {"doc_class": "...", "confidence": 0.0}
"""

USER_TEMPLATE = """First page of the document:

{page_text}
"""
```

- [ ] **Step 4: Implement the runner**

```python
# renewal/classify/runner.py
"""The coarse label, and the routing decision it feeds."""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.classify import prompt_v1
from renewal.config import Settings
from renewal.models import Document, DocumentClassification
from renewal.providers import ModelClient, text_block
from renewal.text.store import page_text

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = prompt_v1.VERSION

DOC_CLASSES = (
    "declarations", "endorsement", "cancellation_notice", "non_renewal_notice",
    "invoice", "id_card", "loss_run", "inspection_report", "quote",
    "correspondence", "unknown",
)

# Only these route on to structured field extraction. quote is classified and
# stored; nothing consumes it in this phase.
FIELD_EXTRACTION_CLASSES = ("declarations", "endorsement")


def parse_classification(raw: str) -> tuple[str, float]:
    """Anything unrecognised collapses to unknown at zero confidence. This
    function never raises: classification gates nothing, so a bad response must
    degrade rather than fail."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return ("unknown", 0.0)
    try:
        payload = json.loads(match.group(0))
        label = payload.get("doc_class")
        confidence = float(payload.get("confidence", 0.0))
    except (ValueError, TypeError):
        return ("unknown", 0.0)
    if label not in DOC_CLASSES:
        return ("unknown", 0.0)
    return (label, confidence)


def latest_class(session: Session, document_id: int) -> str | None:
    return session.scalar(
        select(DocumentClassification.doc_class)
        .where(DocumentClassification.document_id == document_id)
        .order_by(DocumentClassification.id.desc())
        .limit(1)
    )


def classify(
    session: Session, document: Document, *, client: ModelClient, settings: Settings
) -> DocumentClassification:
    text = page_text(session, document.id, 1).strip()
    model_id = f"{settings.provider}:{settings.classification_model}"

    if not text:
        label, confidence = "unknown", 0.0
    else:
        try:
            raw = client.complete(
                model=settings.classification_model,
                system=prompt_v1.SYSTEM,
                content=[text_block(prompt_v1.USER_TEMPLATE.format(page_text=text))],
            )
            label, confidence = parse_classification(raw)
        except Exception:  # noqa: BLE001 - a declined answer is still an answer
            logger.exception("classification failed document_id=%s", document.id)
            label, confidence = "unknown", 0.0

    row = DocumentClassification(
        document_id=document.id, doc_class=label, confidence=confidence,
        classifier_version=CLASSIFIER_VERSION, model_id=model_id,
    )
    session.add(row)
    session.flush()
    logger.info(
        "classified document_id=%s class=%s confidence=%.2f",
        document.id, label, confidence,
    )
    return row
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_classify.py -v`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add renewal/classify/ tests/test_classify.py tests/test_dates_llm.py
git commit -m "feat(classify): coarse document label that gates nothing"
```

---

## Task 2: Classification in the pipeline and the eval run

**Files:**
- Modify: `renewal/pipeline.py`, `evals/test_dates.py`, `evals/accuracy.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `classify`, `latest_class`, `FIELD_EXTRACTION_CLASSES`, `score_classification`.
- Produces: `run_classify_stage(session, document, *, client, settings) -> None`, `should_extract_fields(session, document_id) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py — append
import json

from renewal.classify.runner import latest_class
from renewal.models import DocumentText
from renewal.pipeline import ingest_document, should_extract_fields
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings


def test_ingest_classifies_the_document(session, store):
    stub = StubClient(json.dumps({"doc_class": "declarations", "confidence": 0.9}))
    document = ingest_document(
        session, store, data=make_text_pdf([["DECLARATIONS"]]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=stub, settings=_settings(),
    )
    assert latest_class(session, document.id) == "declarations"


def test_only_declarations_and_endorsements_route_to_field_extraction(session, store):
    for label, expected in (("declarations", True), ("endorsement", True),
                            ("invoice", False), ("unknown", False)):
        stub = StubClient(json.dumps({"doc_class": label, "confidence": 0.9}))
        document = ingest_document(
            session, store, data=make_text_pdf([[f"page for {label}"]]),
            original_filename="d.pdf", source="bulk_import", agency_id=1,
            model_client=stub, settings=_settings(),
        )
        assert should_extract_fields(session, document.id) is expected


def test_a_failed_classification_does_not_stop_text_or_dates(session, store):
    """Classification gates nothing. This is the test that proves it."""
    class Exploding:
        def complete(self, **kwargs):
            raise RuntimeError("provider down")

    document = ingest_document(
        session, store, data=make_text_pdf([["Expiration Date: 07/01/2026"]]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=Exploding(), settings=_settings(),
    )
    from renewal.models import DocumentDate
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1
    assert session.query(DocumentDate).filter_by(document_id=document.id).count() > 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_pipeline.py -k classif -v`
Expected: FAIL with `ImportError: cannot import name 'should_extract_fields'`

- [ ] **Step 3: Add the stage**

In `renewal/pipeline.py`:

```python
from renewal.classify.runner import (
    FIELD_EXTRACTION_CLASSES, classify, latest_class,
)


def run_classify_stage(
    session: Session, document: Document, *, client: ModelClient, settings: Settings
) -> None:
    if latest_class(session, document.id) is not None:
        return
    try:
        classify(session, document, client=client, settings=settings)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("classify stage failed document_id=%s", document.id)


def should_extract_fields(session: Session, document_id: int) -> bool:
    """Routing only. A document that is not routed here is still stored,
    searchable, and date-extracted."""
    return latest_class(session, document_id) in FIELD_EXTRACTION_CLASSES
```

Call `run_classify_stage` from `ingest_document` after `run_dates_stage`. Date
extraction runs first on purpose: it must not be able to depend on a label.

- [ ] **Step 4: Wire classification into the eval run**

In `evals/test_dates.py`, after ingesting each fixture, add:

```python
        results[fixture.fixture_id]["classification"] = score_classification(
            fixture.doc_class, latest_class(session, document.id) or "unknown"
        )
```

and add a `classification_report` to `evals/accuracy.py` that counts
`correct`, `declined`, and `wrong` separately and prints them on three lines.
Declined is reported beside correct, never folded into wrong — a harness that
punishes `unknown` would train exactly the behavior the spec forbids.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_pipeline.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add renewal/pipeline.py evals/ tests/test_pipeline.py
git commit -m "feat(classify): route field extraction by class, report accuracy"
```

---

## Task 3: The provider interface and MIME parsing

The hosted provider is not chosen yet. The interface and a local file-drop implementation ship now so everything downstream is testable and the choice stays a one-file change.

**Files:**
- Create: `renewal/mail/__init__.py`, `renewal/mail/provider.py`, `renewal/mail/filedrop.py`, `renewal/mail/parse.py`
- Test: `tests/test_mail_parse.py` (create)

**Interfaces:**
- Consumes: the standard-library `email` package.
- Produces: `Attachment(filename: str, content_type: str, data: bytes)`; `InboundEmail(message_id, from_address, to_address, subject, received_at, body_text, attachments, raw_mime)`; `InboundProvider` protocol with `verify(headers: dict, body: bytes) -> bool` and `parse(headers: dict, body: bytes) -> InboundEmail`; `FileDropProvider`; `parse_mime(raw: bytes) -> InboundEmail`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_parse.py
from email.message import EmailMessage

from renewal.mail.filedrop import FileDropProvider
from renewal.mail.parse import parse_mime
from tests.pdfmaker import make_text_pdf


def _message(*, with_attachment=True, html_only=False):
    message = EmailMessage()
    message["Message-ID"] = "<abc123@carrier.example>"
    message["From"] = "underwriting@carrier.example"
    message["To"] = "intake+default@example.com"
    message["Subject"] = "Notice of cancellation - Acme Landscaping"
    message["Date"] = "Mon, 01 Jun 2026 09:00:00 -0700"
    if html_only:
        message.set_content("<p>Cancellation effective 07/01/2026.</p>",
                            subtype="html")
    else:
        message.set_content("Cancellation effective 07/01/2026.")
    if with_attachment:
        message.add_attachment(make_text_pdf([["NOTICE OF CANCELLATION"]]),
                               maintype="application", subtype="pdf",
                               filename="notice.pdf")
    return message.as_bytes()


def test_headers_are_read():
    got = parse_mime(_message())
    assert got.message_id == "<abc123@carrier.example>"
    assert got.to_address == "intake+default@example.com"
    assert got.subject.startswith("Notice of cancellation")


def test_the_body_text_is_extracted():
    """The carrier's explanation is frequently in the body while the
    attachment is a bare form."""
    assert "Cancellation effective 07/01/2026" in parse_mime(_message()).body_text


def test_an_html_only_body_is_reduced_to_text():
    body = parse_mime(_message(html_only=True)).body_text
    assert "Cancellation effective 07/01/2026" in body
    assert "<p>" not in body


def test_pdf_attachments_are_extracted():
    attachments = parse_mime(_message()).attachments
    assert [a.filename for a in attachments] == ["notice.pdf"]
    assert attachments[0].data.startswith(b"%PDF")


def test_a_message_with_no_attachment_still_parses():
    got = parse_mime(_message(with_attachment=False))
    assert got.attachments == []
    assert got.body_text


def test_the_raw_mime_is_preserved_verbatim():
    raw = _message()
    assert parse_mime(raw).raw_mime == raw


def test_a_missing_message_id_is_synthesised_from_the_content():
    """Dedupe needs a key. A content hash is stable across re-deliveries of the
    same message, which is exactly the property Message-ID was providing."""
    message = EmailMessage()
    message["From"] = "a@b.example"
    message["To"] = "intake+default@example.com"
    message.set_content("no message id here")
    got = parse_mime(message.as_bytes())
    assert got.message_id.startswith("sha256:")


def test_the_received_date_falls_back_to_now_when_unparseable():
    message = EmailMessage()
    message["Message-ID"] = "<x@y>"
    message["From"] = "a@b.example"
    message["To"] = "intake+default@example.com"
    message["Date"] = "not a date"
    message.set_content("body")
    assert parse_mime(message.as_bytes()).received_at is not None


def test_the_filedrop_provider_reads_an_eml_file(tmp_path):
    path = tmp_path / "one.eml"
    path.write_bytes(_message())
    provider = FileDropProvider(tmp_path)
    assert [e.message_id for e in provider.drain()] == ["<abc123@carrier.example>"]


def test_the_filedrop_provider_verifies_nothing_and_says_so(tmp_path):
    assert FileDropProvider(tmp_path).verify({}, b"") is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_mail_parse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.mail'`

- [ ] **Step 3: Implement the interface**

```python
# renewal/mail/provider.py
"""The inbound-mail boundary.

The hosted provider is deliberately not chosen yet. Postmark inbound,
CloudMailin, and SES inbound all deliver the same two things — the raw MIME and
a set of headers to authenticate the request — so the record layer depends on
this interface rather than on any of them, and picking one is a single new
implementation of `InboundProvider` plus a line of wiring.

When that choice is made, record it in a comment here: which provider, why, and
what its webhook authentication actually verifies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class InboundEmail:
    message_id: str
    from_address: str
    to_address: str
    subject: str | None
    received_at: datetime
    body_text: str
    raw_mime: bytes
    attachments: list[Attachment] = field(default_factory=list)


class InboundProvider(Protocol):
    def verify(self, headers: dict, body: bytes) -> bool:
        """Whether this request genuinely came from the provider. A false
        result must quarantine, never process."""

    def parse(self, headers: dict, body: bytes) -> InboundEmail: ...
```

- [ ] **Step 4: Implement parsing and the file-drop provider**

```python
# renewal/mail/parse.py
"""MIME to the neutral shape the rest of the system uses."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime

from renewal.mail.provider import Attachment, InboundEmail

_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    return " ".join(_TAG.sub(" ", html).split())


def _received_at(message) -> datetime:
    try:
        return parsedate_to_datetime(message["Date"])
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def parse_mime(raw: bytes) -> InboundEmail:
    message = message_from_bytes(raw, policy=policy.default)

    body_parts: list[str] = []
    attachments: list[Attachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        content_type = part.get_content_type()
        if disposition == "attachment" or part.get_filename():
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                Attachment(
                    filename=part.get_filename() or "attachment",
                    content_type=content_type,
                    data=payload,
                )
            )
        elif content_type == "text/plain":
            body_parts.append(part.get_content())
        elif content_type == "text/html":
            body_parts.append(_html_to_text(part.get_content()))

    message_id = message["Message-ID"]
    if not message_id:
        # Dedupe needs a stable key. A content hash re-derives the same value
        # for a re-delivery of the same bytes, which is what Message-ID gave us.
        message_id = f"sha256:{hashlib.sha256(raw).hexdigest()}"

    return InboundEmail(
        message_id=message_id,
        from_address=str(message["From"] or ""),
        to_address=str(message["To"] or ""),
        subject=str(message["Subject"]) if message["Subject"] else None,
        received_at=_received_at(message),
        body_text="\n\n".join(p.strip() for p in body_parts if p.strip()),
        raw_mime=raw,
        attachments=attachments,
    )
```

```python
# renewal/mail/filedrop.py
"""A local implementation for development and tests.

Reads .eml files from a directory. It exists so the whole intake path can be
exercised without a hosted provider, a domain, or an MX record — and so the
tests that matter here are about routing, dedupe, and quarantine rather than
about someone's webhook format.
"""

from __future__ import annotations

from pathlib import Path

from renewal.mail.parse import parse_mime
from renewal.mail.provider import InboundEmail


class FileDropProvider:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def verify(self, headers: dict, body: bytes) -> bool:
        """Nothing to verify: this provider is local files, not a network
        caller. It must never be wired to a public route."""
        return True

    def parse(self, headers: dict, body: bytes) -> InboundEmail:
        return parse_mime(body)

    def drain(self) -> list[InboundEmail]:
        return [
            parse_mime(path.read_bytes())
            for path in sorted(self.directory.glob("*.eml"))
        ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_mail_parse.py -v`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add renewal/mail/ tests/test_mail_parse.py
git commit -m "feat(mail): inbound provider interface, MIME parsing, file drop"
```

---

## Task 4: Intake — route, dedupe, quarantine, store

⚠️ Mail that does not resolve to a known intake address is stored and quarantined, never processed and never silently dropped. Routing is by recipient, not by sender: sender addresses are trivially forged and forwarded mail carries the wrong one anyway.

**Files:**
- Create: `renewal/mail/intake.py`
- Test: `tests/test_mail_intake.py` (create)

**Interfaces:**
- Consumes: `InboundEmail`, `ingest_document`, `BlobStore`, models `Agency`, `InboundMessage`.
- Produces: `receive(session, store, email, *, client, settings) -> InboundMessage`, `agency_for(session, to_address: str) -> Agency | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_intake.py
from datetime import datetime, timezone

from renewal.mail.intake import agency_for, receive
from renewal.mail.provider import Attachment, InboundEmail
from renewal.models import Agency, Document, DocumentText, InboundMessage
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings


def _agency(session, address="intake+default@example.com"):
    """The agency row is configuration on a singleton, not a record of events.
    It is the documented exception to the insert-only rule, alongside the ics
    token: a rotated address must stop routing immediately, which an appended
    row would not achieve."""
    agency = session.query(Agency).filter_by(slug="default").one()
    agency.intake_address = address
    session.flush()
    return agency


def _email(*, to="intake+default@example.com", message_id="<a@b>", attach=True):
    return InboundEmail(
        message_id=message_id, from_address="underwriting@carrier.example",
        to_address=to, subject="Notice of cancellation",
        received_at=datetime.now(timezone.utc),
        body_text="Cancellation effective 07/01/2026.",
        raw_mime=b"raw mime bytes here",
        attachments=[Attachment("notice.pdf", "application/pdf",
                                make_text_pdf([["NOTICE OF CANCELLATION"]]))]
        if attach else [],
    )


def _receive(session, store, email):
    return receive(session, store, email,
                   client=StubClient('{"dates": []}'), settings=_settings())


def test_routing_is_by_recipient(session, store):
    agency = _agency(session)
    assert agency_for(session, "intake+default@example.com").id == agency.id


def test_an_unknown_recipient_is_quarantined_not_dropped(session, store):
    _agency(session)
    message = _receive(session, store, _email(to="intake+nobody@example.com"))
    assert message.processing_status == "quarantined"
    assert session.query(Document).count() == 0
    assert session.get(InboundMessage, message.id) is not None


def test_a_display_name_around_the_address_still_routes(session, store):
    _agency(session)
    message = _receive(session, store,
                       _email(to='"Intake" <intake+default@example.com>'))
    assert message.processing_status == "processed"


def test_the_body_becomes_a_document(session, store):
    """Deadlines are very often stated in prose in the body."""
    _agency(session)
    _receive(session, store, _email())
    bodies = session.query(Document).filter_by(source="email_body").all()
    assert len(bodies) == 1
    assert "Cancellation effective" in session.query(DocumentText).filter_by(
        document_id=bodies[0].id).one().text


def test_attachments_become_documents_linked_to_the_message(session, store):
    _agency(session)
    message = _receive(session, store, _email())
    attachments = session.query(Document).filter_by(
        source="email_attachment").all()
    assert len(attachments) == 1
    assert attachments[0].inbound_message_id == message.id


def test_a_message_with_no_attachment_still_produces_a_body_document(session, store):
    _agency(session)
    _receive(session, store, _email(attach=False))
    assert session.query(Document).filter_by(source="email_body").count() == 1


def test_a_duplicate_message_id_does_no_work(session, store):
    """Forwarded mail arrives multiple times. The second arrival returns the
    row we already have and creates nothing."""
    _agency(session)
    first = _receive(session, store, _email())
    second = _receive(session, store, _email())
    assert second.id == first.id
    assert session.query(InboundMessage).count() == 1
    assert session.query(Document).count() == 2


def test_identical_attachments_across_messages_share_one_blob(session, store):
    _agency(session)
    _receive(session, store, _email(message_id="<one@x>"))
    _receive(session, store, _email(message_id="<two@x>"))
    digests = {d.blob_sha256 for d in session.query(Document).filter_by(
        source="email_attachment")}
    assert len(digests) == 1


def test_the_raw_mime_is_stored_as_a_blob(session, store):
    _agency(session)
    message = _receive(session, store, _email())
    assert store.get(message.raw_mime_blob_sha256, ext="eml") == b"raw mime bytes here"


def test_a_non_pdf_attachment_is_skipped_without_failing_the_message(session, store):
    _agency(session)
    email = _email()
    email.attachments.append(Attachment("logo.png", "image/png", b"\x89PNG..."))
    message = _receive(session, store, email)
    assert message.processing_status == "processed"
    assert session.query(Document).filter_by(source="email_attachment").count() == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_mail_intake.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.mail.intake'`

- [ ] **Step 3: Implement**

```python
# renewal/mail/intake.py
"""Turning a received email into rows.

Routing is by recipient. Each agency has a unique intake address and mail is
matched against it — never against the sender, which is forgeable and which
forwarded mail gets wrong anyway. Mail that matches nothing is stored with
status 'quarantined' and produces no documents: dropping it silently would
make a misconfigured forwarding rule invisible.

The body becomes a document alongside the attachments. The carrier's
explanation is frequently in the body while the attachment is a bare form, and
deadlines are very often stated in prose. Its blob is the raw MIME it shares
with the message row, and its page-1 text is the extracted body, so it flows
through search and date extraction exactly like a PDF does.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.mail.provider import InboundEmail
from renewal.models import Agency, Document, DocumentText, InboundMessage
from renewal.pipeline import (
    run_classify_stage, run_dates_stage, run_resolve_stage, ingest_document,
)
from renewal.providers import ModelClient
from renewal.text.store import TEXT_VERSION

logger = logging.getLogger(__name__)

_ADDRESS = re.compile(r"[\w.+-]+@[\w.-]+")
PDF_TYPES = ("application/pdf", "application/x-pdf")


def agency_for(session: Session, to_address: str) -> Agency | None:
    found = _ADDRESS.search(to_address or "")
    if not found:
        return None
    return session.scalar(
        select(Agency).where(
            func.lower(Agency.intake_address) == found.group(0).casefold()
        )
    )


def receive(
    session: Session,
    store: BlobStore,
    email: InboundEmail,
    *,
    client: ModelClient,
    settings: Settings,
) -> InboundMessage:
    raw_digest = store.put(email.raw_mime, ext="eml")
    agency = agency_for(session, email.to_address)

    if agency is None:
        message = InboundMessage(
            agency_id=None, message_id=email.message_id,
            from_address=email.from_address, to_address=email.to_address,
            subject=email.subject, received_at=email.received_at,
            raw_mime_blob_sha256=raw_digest, body_text="",
            processing_status="quarantined",
        )
        session.add(message)
        session.flush()
        logger.warning(
            "mail quarantined message_row_id=%s reason=unknown_intake_address",
            message.id,
        )
        return message

    existing = session.scalar(
        select(InboundMessage)
        .where(InboundMessage.agency_id == agency.id)
        .where(InboundMessage.message_id == email.message_id)
    )
    if existing is not None:
        # Return the row we already have. Writing a second row would mean
        # forging a message_id to get past the unique constraint, corrupting
        # the exact key dedupe depends on. The blob is already stored and
        # deduplicated by content, so a re-delivery costs nothing.
        logger.info(
            "mail duplicate message_row_id=%s agency_id=%s", existing.id, agency.id
        )
        return existing

    message = InboundMessage(
        agency_id=agency.id, message_id=email.message_id,
        from_address=email.from_address, to_address=email.to_address,
        subject=email.subject, received_at=email.received_at,
        raw_mime_blob_sha256=raw_digest, body_text=email.body_text,
        processing_status="received",
    )
    session.add(message)
    session.flush()

    body_document = Document(
        blob_sha256=raw_digest, original_filename=f"{email.subject or 'message'}.eml",
        page_count=1, has_text_layer=True, doc_type="email_body",
        source="email_body", agency_id=agency.id, inbound_message_id=message.id,
    )
    session.add(body_document)
    session.flush()
    session.add(
        DocumentText(
            document_id=body_document.id, page_number=1, text=email.body_text,
            extraction_method="email_body", extractor_version=TEXT_VERSION,
        )
    )
    session.flush()
    run_resolve_stage(session, body_document)
    run_dates_stage(session, body_document, client=client, settings=settings)
    run_classify_stage(session, body_document, client=client, settings=settings)

    for attachment in email.attachments:
        if attachment.content_type not in PDF_TYPES:
            logger.info(
                "attachment skipped message_row_id=%s content_type=%s",
                message.id, attachment.content_type,
            )
            continue
        ingest_document(
            session, store, data=attachment.data,
            original_filename=attachment.filename, source="email_attachment",
            agency_id=agency.id, inbound_message_id=message.id,
            model_client=client, settings=settings,
        )

    message.processing_status = "processed"
    session.flush()
    return message
```

The `processing_status` assignment at the end is the one place a row written by
this module is updated, and it happens inside the same transaction that created
it — the row is never observed in its intermediate state. `received` exists so
that a crash mid-processing leaves evidence of what was in flight rather than a
row claiming success.

The body document sets `doc_type="email_body"`, distinct from the `dec_page`
marker the existing structured extractor looks for, so an email body can never
be picked up by that path.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_mail_intake.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/mail/intake.py tests/test_mail_intake.py
git commit -m "feat(mail): route by recipient, quarantine strangers, store the body"
```

---

## Task 5: The webhook

**Files:**
- Create: `renewal/web/mail.py`
- Modify: `renewal/web/__init__.py`, `renewal/config.py`, `.env.example`
- Test: `tests/test_web_mail.py` (create)

**Interfaces:**
- Consumes: `InboundProvider`, `receive`.
- Produces: route `POST /inbound/mail`. `Settings.inbound_provider: str`, `Settings.inbound_drop_dir: str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_mail.py
from renewal.models import Document, InboundMessage


def test_a_valid_message_returns_200_and_is_stored(client_app, agency_intake, eml):
    response = client_app.post("/inbound/mail", content=eml)
    assert response.status_code == 200


def test_the_attachments_and_body_become_documents(client_app, agency_intake, eml, db):
    client_app.post("/inbound/mail", content=eml)
    sources = {d.source for d in db.query(Document).all()}
    assert sources == {"email_body", "email_attachment"}


def test_an_unknown_recipient_still_returns_200_but_quarantines(
    client_app, agency_intake, eml_to_stranger, db
):
    """A non-200 makes the provider retry forever. Accept and quarantine."""
    assert client_app.post("/inbound/mail", content=eml_to_stranger).status_code == 200
    assert db.query(InboundMessage).one().processing_status == "quarantined"
    assert db.query(Document).count() == 0


def test_a_failed_verification_is_rejected_without_processing(
    client_app_unverified, eml, db
):
    assert client_app_unverified.post("/inbound/mail", content=eml).status_code == 403
    assert db.query(InboundMessage).count() == 0


def test_unparseable_mime_returns_200_and_records_the_failure(
    client_app, agency_intake, db
):
    client_app.post("/inbound/mail", content=b"this is not MIME at all")
    assert db.query(InboundMessage).one().processing_status in ("failed", "quarantined")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_mail.py -v`
Expected: FAIL with 404 on `/inbound/mail`

- [ ] **Step 3: Add the settings**

In `renewal/config.py`, add `inbound_provider: str = "filedrop"` and
`inbound_drop_dir: str = "mail"`, read from `INBOUND_PROVIDER` and
`INBOUND_DROP_DIR`. Document in `.env.example` that `filedrop` is for local
development only and must never be the configured provider on a
network-reachable install, because it verifies nothing.

- [ ] **Step 4: Implement the route**

`renewal/web/mail.py`:

- `POST /inbound/mail` — read the raw body and headers. Call
  `provider.verify(headers, body)`; on a false result return **403 and write
  nothing**, because an unverified caller is not evidence about anything.
- On success, call `provider.parse` and then `receive` in a background task,
  returning 200 immediately. A non-200 makes a hosted provider retry the
  delivery indefinitely, so every outcome that is not a failed verification —
  an unknown recipient, a duplicate, unparseable MIME — returns 200 and is
  recorded in `processing_status` instead.
- Wrap `parse` in a try/except that writes an `InboundMessage` with
  `processing_status="failed"`, the raw MIME blob, and no agency. The raw bytes
  are kept so the failure can be diagnosed and the message re-processed after a
  parser fix.

Log the message row id and status. Never the body, never the subject.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_mail.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add renewal/web/mail.py renewal/config.py .env.example tests/test_web_mail.py
git commit -m "feat(web): inbound mail webhook behind the provider interface"
```

---

## Task 6: Attention rules

Not a task manager. A list of documents that appear to need a human response, with a suggested reason. Bias toward over-flagging: missing an item is the dangerous failure.

**Files:**
- Create: `renewal/attention/__init__.py`, `renewal/attention/rules.py`
- Modify: `renewal/pipeline.py`, `renewal/config.py`
- Test: `tests/test_attention.py` (create)

**Interfaces:**
- Consumes: `latest_class`, `latest_link`, models `AttentionItem`, `AttentionEvent`, `DocumentDate`, `DateEvent`, `PolicyTerm`.
- Produces: `REASONS: tuple[str, ...]`, `UNCONFIRMED_DATE_WINDOW_DAYS = 14`, `evaluate(session, document, *, settings) -> list[AttentionItem]`, `open_items(session, *, today=None) -> list[QueueRow]`, `resolve(session, item_id, *, action, actor="human") -> AttentionEvent`. `run_attention_stage(session, document, *, settings)` in `pipeline`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attention.py
import json
from datetime import date, timedelta

from renewal.attention.rules import (
    UNCONFIRMED_DATE_WINDOW_DAYS, evaluate, open_items, resolve,
)
from renewal.models import AttentionItem, Client, DocumentDate, DocumentLink
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings

TODAY = date(2026, 6, 1)


def _document(session, store, lines, doc_class="unknown"):
    from renewal.pipeline import ingest_document
    return ingest_document(
        session, store, data=make_text_pdf([lines]), original_filename="d.pdf",
        source="bulk_import", agency_id=1,
        model_client=StubClient(json.dumps({"doc_class": doc_class,
                                            "confidence": 0.9})),
        settings=_settings(),
    )


def test_a_cancellation_notice_is_flagged(session, store):
    document = _document(session, store, ["NOTICE OF CANCELLATION"],
                         doc_class="cancellation_notice")
    items = evaluate(session, document, settings=_settings())
    assert "cancellation_notice" in {i.reason_code for i in items}


def test_a_non_renewal_notice_is_flagged(session, store):
    document = _document(session, store, ["NOTICE OF NON-RENEWAL"],
                         doc_class="non_renewal_notice")
    assert "non_renewal_notice" in {
        i.reason_code for i in evaluate(session, document, settings=_settings())}


def test_an_unmatched_document_is_flagged(session, store):
    document = _document(session, store, ["a page nobody can place"])
    assert "unmatched_document" in {
        i.reason_code for i in evaluate(session, document, settings=_settings())}


def test_a_matched_document_is_not_flagged_as_unmatched(session, store):
    document = _document(session, store, ["a page"])
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="manual", confidence=1.0, candidates=[]))
    session.flush()
    assert "unmatched_document" not in {
        i.reason_code for i in evaluate(session, document, settings=_settings())}


def test_an_ordinary_invoice_is_not_flagged(session, store):
    """Over-flagging is the bias, not flagging everything."""
    document = _document(session, store, ["INVOICE"], doc_class="invoice")
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="manual", confidence=1.0, candidates=[]))
    session.flush()
    assert evaluate(session, document, settings=_settings()) == []


def test_evaluation_is_idempotent(session, store):
    document = _document(session, store, ["NOTICE OF CANCELLATION"],
                         doc_class="cancellation_notice")
    evaluate(session, document, settings=_settings())
    evaluate(session, document, settings=_settings())
    assert session.query(AttentionItem).filter_by(
        document_id=document.id, reason_code="cancellation_notice").count() == 1


def test_a_near_unconfirmed_date_appears_in_the_queue_without_a_row(session, store):
    """It changes with the clock, so it is computed at read time rather than
    materialised — which would need a daemon we are not building."""
    document = _document(session, store, ["a page"])
    session.add(DocumentDate(
        document_id=document.id, date_value=TODAY + timedelta(days=7),
        date_type="cancellation_effective", source_page=1, source_text="x",
        confidence=0.5, extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    reasons = {r.reason_code for r in open_items(session, today=TODAY)}
    assert "unconfirmed_date_within_14_days" in reasons
    assert session.query(AttentionItem).filter_by(
        reason_code="unconfirmed_date_within_14_days").count() == 0


def test_a_date_beyond_the_window_is_not_in_the_queue(session, store):
    document = _document(session, store, ["a page"])
    session.add(DocumentDate(
        document_id=document.id,
        date_value=TODAY + timedelta(days=UNCONFIRMED_DATE_WINDOW_DAYS + 1),
        date_type="policy_expiration", source_page=1, source_text="x",
        confidence=0.5, extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    assert "unconfirmed_date_within_14_days" not in {
        r.reason_code for r in open_items(session, today=TODAY)}


def test_a_confirmed_date_leaves_the_queue(session, store):
    from renewal.dates.service import confirm
    document = _document(session, store, ["a page"])
    row = DocumentDate(document_id=document.id,
                       date_value=TODAY + timedelta(days=7),
                       date_type="policy_expiration", source_page=1,
                       source_text="x", confidence=0.5,
                       extractor_version="dates-regex-v1", pass_name="regex")
    session.add(row)
    session.flush()
    confirm(session, row.id)
    assert "unconfirmed_date_within_14_days" not in {
        r.reason_code for r in open_items(session, today=TODAY)}


def test_resolving_appends_an_event_and_clears_the_item(session, store):
    document = _document(session, store, ["NOTICE OF CANCELLATION"],
                         doc_class="cancellation_notice")
    item = evaluate(session, document, settings=_settings())[0]
    resolve(session, item.id, action="done")
    assert item.id not in {r.item_id for r in open_items(session, today=TODAY)}


def test_nothing_resolves_itself(session, store):
    """We cannot detect that she replied without reading sent mail, which needs
    OAuth this project does not do."""
    document = _document(session, store, ["NOTICE OF CANCELLATION"],
                         doc_class="cancellation_notice")
    item = evaluate(session, document, settings=_settings())[0]
    evaluate(session, document, settings=_settings())
    assert item.id in {r.item_id for r in open_items(session, today=TODAY)}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_attention.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.attention'`

- [ ] **Step 3: Implement**

```python
# renewal/attention/rules.py
"""What appears to need a human response.

Deliberately not a task manager. Every item is a document plus a suggested
reason, and every item is cleared by hand. Nothing here auto-resolves and
nothing here acts: detecting that she replied would require reading sent mail,
which requires OAuth this project does not do.

Rules bias toward over-flagging. A wrong flag costs one keystroke to dismiss; a
missed cancellation notice is the risk this whole system exists to reduce.

Event-triggered reasons are rows, written once at ingest. The one time-based
reason — an unconfirmed date coming up soon — is computed at read time instead,
because it changes with the clock and materialising it would need a scheduler
this design does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.classify.runner import latest_class
from renewal.models import (
    AttentionEvent, AttentionItem, Client, DateEvent, Document, DocumentDate,
    DocumentLink,
)
from renewal.resolve.service import latest_link

UNCONFIRMED_DATE_WINDOW_DAYS = 14

REASONS = (
    "cancellation_notice",
    "non_renewal_notice",
    "renewal_received",
    "premium_change",
    "unmatched_document",
    "unconfirmed_date_within_14_days",
)

_CLASS_REASONS = {
    "cancellation_notice": "Classified as a cancellation notice",
    "non_renewal_notice": "Classified as a non-renewal notice",
}


@dataclass(frozen=True)
class QueueRow:
    item_id: int | None
    document_id: int
    client_id: int | None
    client_name: str | None
    reason_code: str
    reason_text: str
    due_date: date | None
    materialised: bool


def _has_item(session: Session, document_id: int, reason_code: str) -> bool:
    return session.scalar(
        select(AttentionItem.id)
        .where(AttentionItem.document_id == document_id)
        .where(AttentionItem.reason_code == reason_code)
        .limit(1)
    ) is not None


def _add(
    session: Session, document_id: int, reason_code: str, reason_text: str,
    due_date: date | None = None,
) -> AttentionItem | None:
    if _has_item(session, document_id, reason_code):
        return None
    item = AttentionItem(document_id=document_id, reason_code=reason_code,
                         reason_text=reason_text, due_date=due_date)
    session.add(item)
    session.flush()
    return item


def evaluate(session: Session, document: Document, *, settings) -> list[AttentionItem]:
    """Event-triggered reasons only. Idempotent: re-running never duplicates an
    item, and never revives one she has resolved."""
    created: list[AttentionItem] = []
    doc_class = latest_class(session, document.id)

    if doc_class in _CLASS_REASONS:
        item = _add(session, document.id, doc_class, _CLASS_REASONS[doc_class])
        if item:
            created.append(item)

    if latest_link(session, document.id) is None:
        item = _add(session, document.id, "unmatched_document",
                    "Could not be attached to a client")
        if item:
            created.append(item)

    return created


def open_items(session: Session, *, today: date | None = None) -> list[QueueRow]:
    today = today or date.today()
    resolved = select(AttentionEvent.attention_item_id).distinct()

    rows: list[QueueRow] = []
    query = (
        select(AttentionItem, DocumentLink.client_id, Client.display_name)
        .outerjoin(DocumentLink,
                   DocumentLink.document_id == AttentionItem.document_id)
        .outerjoin(Client, Client.id == DocumentLink.client_id)
        .where(AttentionItem.id.not_in(resolved))
        .order_by(AttentionItem.id.desc())
    )
    seen: set[int] = set()
    for item, client_id, client_name in session.execute(query):
        if item.id in seen:
            continue
        seen.add(item.id)
        rows.append(QueueRow(
            item_id=item.id, document_id=item.document_id, client_id=client_id,
            client_name=client_name, reason_code=item.reason_code,
            reason_text=item.reason_text, due_date=item.due_date,
            materialised=True,
        ))

    judged = select(DateEvent.document_date_id).distinct()
    horizon = today + timedelta(days=UNCONFIRMED_DATE_WINDOW_DAYS)
    near = (
        select(DocumentDate, DocumentLink.client_id, Client.display_name)
        .outerjoin(DocumentLink,
                   DocumentLink.document_id == DocumentDate.document_id)
        .outerjoin(Client, Client.id == DocumentLink.client_id)
        .where(DocumentDate.id.not_in(judged))
        .where(DocumentDate.date_value >= today)
        .where(DocumentDate.date_value <= horizon)
        .order_by(DocumentDate.date_value)
    )
    for row, client_id, client_name in session.execute(near):
        rows.append(QueueRow(
            item_id=None, document_id=row.document_id, client_id=client_id,
            client_name=client_name,
            reason_code="unconfirmed_date_within_14_days",
            reason_text=f"Unconfirmed {row.date_type.replace('_', ' ')}",
            due_date=row.date_value, materialised=False,
        ))
    return rows


def resolve(
    session: Session, item_id: int, *, action: str, actor: str = "human"
) -> AttentionEvent:
    event = AttentionEvent(attention_item_id=item_id, action=action, actor=actor)
    session.add(event)
    session.flush()
    return event
```

`renewal_received` and `premium_change` are declared in `REASONS` but not
implemented here. `renewal_received` needs a definition of what distinguishes a
renewal dec page from a new-business bind, which is domain knowledge not yet
supplied — implementing a guess would produce a rule that is confidently wrong.
`premium_change` fires when a comparison is built, which is the step-10 refactor
and its own plan. Both are named now so the reason vocabulary is stable.

Add `run_attention_stage(session, document, *, settings)` to
`renewal/pipeline.py`, wrapped like the other stages, called last in
`ingest_document`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_attention.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/attention/ renewal/pipeline.py tests/test_attention.py
git commit -m "feat(attention): flag documents that appear to need a response"
```

---

## Task 7: The attention queue screen

**Files:**
- Create: `renewal/web/attention.py`, `renewal/templates/attention.html`
- Modify: `renewal/web/__init__.py`, `renewal/templates/client.html`, `renewal/static/app.js`
- Test: `tests/test_web_attention.py` (create)

**Interfaces:**
- Consumes: `open_items`, `resolve`, `dismiss` from `renewal.dates.service`.
- Produces: routes `GET /attention`, `POST /attention/{id}/done`, `POST /attention/{id}/dismiss`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_attention.py
from renewal.models import AttentionEvent, DateEvent


def test_the_queue_lists_open_items(client_app, seeded_cancellation_item):
    body = client_app.get("/attention").text
    assert "cancellation notice" in body.lower()


def test_marking_done_appends_an_event(client_app, seeded_cancellation_item, db):
    response = client_app.post(f"/attention/{seeded_cancellation_item}/done",
                               follow_redirects=False)
    assert response.status_code == 303
    assert db.query(AttentionEvent).filter_by(
        attention_item_id=seeded_cancellation_item, action="done").count() == 1


def test_a_resolved_item_leaves_the_queue(client_app, seeded_cancellation_item):
    client_app.post(f"/attention/{seeded_cancellation_item}/done",
                    follow_redirects=False)
    assert "cancellation notice" not in client_app.get("/attention").text.lower()


def test_dismissing_a_live_date_row_writes_a_date_event(
    client_app, seeded_near_date, db
):
    """The live rule has no attention_item to resolve, so dismissal acts on the
    date itself — which is the right action anyway."""
    client_app.post(f"/dates/{seeded_near_date}/dismiss", follow_redirects=False)
    assert db.query(DateEvent).filter_by(document_date_id=seeded_near_date,
                                         action="dismissed").count() == 1


def test_an_escalated_reason_is_visually_distinct(
    client_app, seeded_cancellation_item
):
    assert "escalated" in client_app.get("/attention").text


def test_an_empty_queue_says_so(client_app):
    assert "nothing needs attention" in client_app.get("/attention").text.lower()


def test_the_client_overview_shows_that_clients_items(
    client_app, seeded_client_with_item
):
    body = client_app.get(f"/clients/{seeded_client_with_item}").text
    assert "cancellation notice" in body.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_attention.py -v`
Expected: FAIL with 404 on `/attention`

- [ ] **Step 3: Implement the routes**

`renewal/web/attention.py`:

- `GET /attention` — `open_items(session)`, rendered with escalated reasons
  (`cancellation_notice`, `non_renewal_notice`) pinned to the top and carrying
  the same `escalated` class the calendar uses.
- `POST /attention/{id}/done` and `/dismiss` — append an `AttentionEvent`,
  redirect 303 back to the queue.
- A row with `materialised=False` has no item id. Its resolve control posts to
  the existing `/dates/{id}/dismiss` route instead, so a live row is cleared by
  acting on the date it came from.

- [ ] **Step 4: Write the template and extend the overview**

`attention.html` extends `base.html`: one dense row per item — client name (or
"unmatched"), reason, due date if any, a link to the document, and done and
dismiss buttons. Empty state reads "Nothing needs attention", not an empty
table.

In `client.html`, the attention and recent-messages sections built in Plan B
now have data. Confirm both render, and that the attention section shows only
that client's open items.

Extend `renewal/static/app.js` so the queue takes the same `j`/`k`/`d`
keystrokes the agenda does, reusing the existing handler rather than adding a
second one.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_attention.py tests/test_web_clients.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add renewal/web/attention.py renewal/templates/ renewal/static/app.js tests/test_web_attention.py
git commit -m "feat(web): the attention queue, cleared only by hand"
```

---

## Done when

- [ ] `pytest tests/ -v` passes.
- [ ] A `.eml` dropped through the file-drop provider produces a body document and an attachment document, both searchable, both date-extracted.
- [ ] Mail to an unknown intake address is visible as quarantined and produced no documents.
- [ ] Classification accuracy and the `unknown` rate are on screen from `pytest -m eval evals/test_dates.py -s`.
- [ ] Nothing in the log output contains a message body, a subject, or document text.

## Deferred, deliberately

- **The hosted mail provider.** Chosen when the domain is registered; document the choice in `renewal/mail/provider.py`.
- **`renewal_received`.** Needs a definition of what distinguishes a renewal dec page from a new-business bind.
- **`premium_change`.** Fires on comparison build, which is the step-10 refactor.
