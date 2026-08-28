# Renewal Comparison Tool v0 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Given a prior-term and a renewal declarations page, extract both with full provenance, diff them, classify each difference by materiality, and produce a draft client explanation a licensed agent reviews and edits.

**Architecture:** The pipeline is a plain Python library (`renewal/`); FastAPI routes and the eval harness are thin callers that parse input and call the library. All persistence is insert-only: corrections, re-extractions, re-promotions, and draft edits all write new rows. Documents live in a content-addressed blob store keyed by sha256; the database is the only index.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, PostgreSQL, SQLAlchemy 2.0 + Alembic, PyMuPDF, Anthropic API, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-renewal-comparison-tool-design.md`

## Global Constraints

- Python 3.11+. PostgreSQL only — never SQLite, including in tests.
- **Every table is insert-only.** No `UPDATE`, no `DELETE`, anywhere in application code. Corrections, re-extraction, re-promotion, and draft edits all insert new rows.
- **Nothing is sent automatically.** No email, no outbound delivery of any kind. Output is a draft on a screen.
- Every extracted field carries value, confidence, `source_page`, and verbatim `source_text_span`. A field whose `source_text` cannot be found on its cited page is kept with `confidence = 0.0` and `validation_error` set — never dropped.
- Extraction models: `claude-opus-5`. Draft model: `claude-sonnet-5`. Temperature 0 for both. The model actually used is recorded in `extraction.model_id`.
- Confidence threshold: `0.80` (configurable). Below it, a field is flagged `needs_review` and blocks promotion.
- Blob path layout: `blobs/<first2>/<next2>/<full-sha256>.pdf`. Never re-write an existing hash.
- **Never log extracted content.** Log document ids and blob hashes only.
- `.gitignore` covers `blobs/` and `evals/pdfs/`. Real client PDFs never enter the repo.
- v0 targets one carrier's personal auto dec page. No abstraction for unseen carriers or lines of business.
- Do not build: auth, accounts, billing, email, ACORD forms, carrier APIs, a component library, mobile layouts, job queues, websockets, Docker, or CI.

---

## File Structure

| File | Responsibility |
|---|---|
| `renewal/blobstore.py` | Content-addressed bytes in, bytes out. Two public methods. |
| `renewal/config.py` | Settings from environment. |
| `renewal/db.py` | Engine and session lifecycle. |
| `renewal/models.py` | SQLAlchemy models, one per spec table. |
| `renewal/fieldpath.py` | Field-path grammar, item keys, glob matching. Shared by extraction, diff, and rules. |
| `renewal/pdftext.py` | PyMuPDF: text-layer detection, layout text, rasterization. |
| `renewal/ingest.py` | Bytes → blob + `document` row. |
| `renewal/extract/schema.py` | Pydantic contract for the model's JSON response. |
| `renewal/extract/prompt_v1.py` | Frozen prompt text for extractor version `v1`. |
| `renewal/extract/validate.py` | Source-text verification and confidence gating. |
| `renewal/extract/runner.py` | `extract(document, version)` → persisted `Extraction`. |
| `renewal/corrections.py` | Record corrections; resolve effective values. |
| `renewal/promote.py` | Extraction + corrections → frozen `policy_term` snapshot. |
| `renewal/diff.py` | Term pair → raw differences. |
| `renewal/materiality.py` | YAML rule set → classification. |
| `renewal/premium.py` | Arithmetic premium attribution + residual. |
| `renewal/draft.py` | Differences + attribution → draft text. |
| `renewal/comparison.py` | Assemble a comparison: diff, classify, log reclassifications. |
| `renewal/web.py` | FastAPI app. Thin routes only. |
| `renewal/app.py` | Production wiring for uvicorn. |
| `renewal/templates/` | Jinja2 templates. |
| `config/materiality.yaml` | The rule set. |
| `scripts/export_corrections.py` | Corrections → labeled dataset. |
| `scripts/reextract.py` | Re-run any extractor version over every blob. |
| `scripts/compare_versions.py` | Per-field accuracy diff between two extractor versions. |
| `evals/` | Fixtures, baseline, harness. |
| `conftest.py` | Shared pytest fixtures, at the repo root so `evals/` sees them. |

---

## Task 1: Blob store

**Files:**
- Create: `pyproject.toml`, `renewal/__init__.py`, `renewal/blobstore.py`
- Test: `tests/test_blobstore.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `BlobStore(root: Path)` with `put(data: bytes) -> str` (sha256 hex), `get(sha256: str) -> bytes`, `path_for(sha256: str) -> Path`; exception `BlobNotFound`.

- [x] **Step 1: Create the project skeleton**

`pyproject.toml`:

```toml
[project]
name = "renewal"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "jinja2>=3.1",
  "python-multipart>=0.0.9",
  "sqlalchemy>=2.0",
  "alembic>=1.13",
  "psycopg[binary]>=3.2",
  "pymupdf>=1.24",
  "anthropic>=0.40",
  "pydantic>=2.8",
  "pyyaml>=6.0",
  "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "httpx>=0.27"]

[tool.pytest.ini_options]
markers = ["eval: hits the real Anthropic API; excluded from the default run"]
addopts = "-m 'not eval'"
testpaths = ["tests", "evals"]
pythonpath = ["."]
```

Then:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
touch renewal/__init__.py tests/__init__.py
```

`tests/__init__.py` matters: `evals/` imports `tests.pdfmaker`, which only works
if `tests` is a package and the repo root is on the path.

- [x] **Step 2: Write the failing tests**

`tests/test_blobstore.py`:

```python
import hashlib

from renewal.blobstore import BlobNotFound, BlobStore
import pytest

PDF = b"%PDF-1.7 fake bytes"


def test_put_returns_sha256_hex(tmp_path):
    store = BlobStore(tmp_path)
    assert store.put(PDF) == hashlib.sha256(PDF).hexdigest()


def test_put_writes_two_level_sharded_path(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(PDF)
    expected = tmp_path / digest[:2] / digest[2:4] / f"{digest}.pdf"
    assert expected.is_file()


def test_put_is_idempotent_and_does_not_rewrite(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(PDF)
    before = store.path_for(digest).stat().st_mtime_ns
    assert store.put(PDF) == digest
    assert store.path_for(digest).stat().st_mtime_ns == before


def test_get_round_trips(tmp_path):
    store = BlobStore(tmp_path)
    assert store.get(store.put(PDF)) == PDF


def test_get_unknown_hash_raises(tmp_path):
    with pytest.raises(BlobNotFound):
        BlobStore(tmp_path).get("0" * 64)
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_blobstore.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.blobstore'`

- [x] **Step 4: Write the implementation**

`renewal/blobstore.py`:

```python
"""Content-addressed blob storage.

The filesystem is dumb storage; the database is the index. A blob's name is
its own sha256, so the store is immutable by construction and identical
documents deduplicate for free.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class BlobNotFound(KeyError):
    """Raised when a hash has no blob behind it."""


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.pdf"

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.path_for(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return digest

    def get(self, sha256: str) -> bytes:
        path = self.path_for(sha256)
        if not path.exists():
            raise BlobNotFound(sha256)
        return path.read_bytes()
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_blobstore.py -v`
Expected: 5 passed

- [x] **Step 6: Commit**

```bash
git add pyproject.toml renewal/__init__.py renewal/blobstore.py tests/test_blobstore.py
git commit -m "feat: content-addressed blob store with dedup"
```

---

## Task 2: Schema, migrations, config

**Files:**
- Create: `renewal/config.py`, `renewal/db.py`, `renewal/models.py`, `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_initial.py`, `conftest.py`, `.env.example`, `PRIVACY.md`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` (fields `database_url`, `blob_root`, `anthropic_api_key`, `extraction_model`, `draft_model`, `confidence_threshold`, `materiality_config`) and `load_settings() -> Settings`; `Base`, `get_engine()`, `session_scope()`; models `Client`, `Policy`, `PolicyTerm`, `Coverage`, `InsuredItem`, `Document`, `Extraction`, `ExtractedField`, `Correction`, `RenewalRun`, `Comparison`, `Difference`, `Reclassification`, `Draft`; pytest fixtures `engine`, `session`, `store`.

- [x] **Step 1: Write config, env template, and privacy note**

`renewal/config.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    blob_root: Path
    anthropic_api_key: str
    extraction_model: str
    draft_model: str
    confidence_threshold: float
    materiality_config: Path


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+psycopg:///renewal"
        ),
        blob_root=Path(os.environ.get("BLOB_ROOT", "blobs")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        extraction_model=os.environ.get("EXTRACTION_MODEL", "claude-opus-5"),
        draft_model=os.environ.get("DRAFT_MODEL", "claude-sonnet-5"),
        confidence_threshold=float(os.environ.get("CONFIDENCE_THRESHOLD", "0.80")),
        materiality_config=Path(
            os.environ.get("MATERIALITY_CONFIG", "config/materiality.yaml")
        ),
    )
```

`.env.example`:

```
DATABASE_URL=postgresql+psycopg:///renewal
TEST_DATABASE_URL=postgresql+psycopg:///renewal_test
BLOB_ROOT=blobs
ANTHROPIC_API_KEY=
EXTRACTION_MODEL=claude-opus-5
DRAFT_MODEL=claude-sonnet-5
CONFIDENCE_THRESHOLD=0.80
MATERIALITY_CONFIG=config/materiality.yaml
```

`PRIVACY.md`:

```markdown
# Privacy

This tool processes real client insurance documents. They contain names,
addresses, VINs, and sometimes dates of birth.

## Documents are sent to a third-party model API

Declarations pages uploaded to this tool are transmitted to Anthropic's API for
extraction and for drafting the client explanation. Documents with a text layer
are sent as text; scanned documents are sent as page images. This is the single
most important thing to disclose to an agency before they use the tool.

## What is stored, and where

- Original PDFs are stored on the local filesystem, named by their sha256 hash.
- Extracted values, corrections, comparisons, and drafts are stored in a local
  PostgreSQL database.
- Nothing is deleted or overwritten. Records are retained indefinitely, which is
  deliberate: state record-retention rules and E&O defense both depend on the
  file being complete.

## What is never done

- No document is ever sent to a client automatically. Every outbound explanation
  is a draft that a licensed human reads and edits.
- Extracted content is never written to application logs. Logs contain document
  ids and blob hashes only.
- Real PDFs and the blob store are excluded from version control.
```

- [x] **Step 2: Write the failing test and the test fixtures**

`conftest.py` at the repo root — not under `tests/`, because `evals/` needs the
same fixtures:

```python
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg:///renewal_test"
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """Each test runs in a transaction that is rolled back afterwards."""
    conn = engine.connect()
    trans = conn.begin()
    sess = sessionmaker(bind=conn)()
    yield sess
    sess.close()
    trans.rollback()
    conn.close()


@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path / "blobs")


TABLES = (
    "client, policy, policy_term, coverage, insured_item, document, extraction,"
    " extracted_field, correction, renewal_run, comparison, difference,"
    " reclassification, draft"
)


@pytest.fixture
def clean_db(engine):
    """For tests that commit (the web tests). The `session` fixture rolls back,
    but a committing test would otherwise leak rows into the next one."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield
```

`tests/test_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import (
    Client,
    Correction,
    Coverage,
    Document,
    Extraction,
    InsuredItem,
    Policy,
    PolicyTerm,
)


def _policy(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    return policy


def test_term_chain_persists(session):
    policy = _policy(session)
    doc = Document(
        blob_sha256="a" * 64,
        original_filename="dec.pdf",
        page_count=2,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(doc)
    session.flush()
    term = PolicyTerm(
        policy_id=policy.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        effective_date="2026-03-01",
        expiration_date="2026-09-01",
        total_premium="1840.00",
        source_document_id=doc.id,
    )
    session.add(term)
    session.flush()
    assert term.id is not None


def test_coverage_item_id_is_nullable_for_policy_level(session):
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id, effective_date="2026-03-01")
    session.add(term)
    session.flush()

    session.add(
        Coverage(
            policy_term_id=term.id,
            coverage_code="BI",
            limit_value="100/300",
        )
    )
    item = InsuredItem(
        policy_term_id=term.id, item_type="vehicle", descriptor="2018 Ford F-150"
    )
    session.add(item)
    session.flush()
    session.add(
        Coverage(
            policy_term_id=term.id,
            insured_item_id=item.id,
            coverage_code="COLL",
            deductible_value="500",
        )
    )
    session.flush()
    codes = {c.coverage_code: c.insured_item_id for c in term.coverages}
    assert codes == {"BI": None, "COLL": item.id}


def test_correction_allows_null_field_id_for_omissions(session):
    doc = Document(
        blob_sha256="b" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(doc)
    session.flush()
    extraction = Extraction(
        document_id=doc.id, extractor_version="v1", model_id="claude-opus-5", status="ok"
    )
    session.add(extraction)
    session.flush()
    session.add(
        Correction(
            extraction_id=extraction.id,
            extracted_field_id=None,
            field_path="coverage.UMBI.limit_value",
            kind="omission",
            corrected_value="100/300",
        )
    )
    session.flush()


def test_correction_kind_is_constrained(session):
    doc = Document(
        blob_sha256="c" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(doc)
    session.flush()
    extraction = Extraction(
        document_id=doc.id, extractor_version="v1", model_id="claude-opus-5", status="ok"
    )
    session.add(extraction)
    session.flush()
    session.add(
        Correction(
            extraction_id=extraction.id,
            field_path="policy.total_premium",
            kind="typo",
            corrected_value="1",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.models'`

- [x] **Step 4: Write the models**

`renewal/models.py`:

```python
"""Every table here is insert-only. Application code never issues UPDATE or
DELETE: corrections, re-extractions, re-promotions, and draft edits all insert
new rows. Values extracted from documents are stored as text exactly as read;
typed parsing happens in the diff layer.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Client(Base):
    __tablename__ = "client"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    policies: Mapped[list["Policy"]] = relationship(back_populates="client")


class Policy(Base):
    """Identity, chosen by a human at upload. carrier_name and policy_number
    here are the label for the whole chain; the per-term values extracted from
    each document live on PolicyTerm."""

    __tablename__ = "policy"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    carrier_name: Mapped[str] = mapped_column(Text)
    policy_number: Mapped[str] = mapped_column(Text)
    line_of_business: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    client: Mapped[Client] = relationship(back_populates="policies")
    terms: Mapped[list["PolicyTerm"]] = relationship(back_populates="policy")


class PolicyTerm(Base):
    """A frozen snapshot promoted from one extraction plus the corrections
    standing at that moment. Never updated: a later correction promotes a new
    row."""

    __tablename__ = "policy_term"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    carrier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_premium: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.id"), nullable=True
    )
    promoted_from_extraction_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction.id"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    policy: Mapped[Policy] = relationship(back_populates="terms")
    coverages: Mapped[list["Coverage"]] = relationship(back_populates="term")
    items: Mapped[list["InsuredItem"]] = relationship(back_populates="term")


class Coverage(Base):
    """insured_item_id NULL means policy-level (BI/PD, UM/UIM). Set means the
    coverage belongs to that vehicle (comp, collision), each with its own
    deductible and premium."""

    __tablename__ = "coverage"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    insured_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("insured_item.id"), nullable=True
    )
    coverage_code: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    limit_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    limit_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    deductible_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    premium: Mapped[str | None] = mapped_column(Text, nullable=True)
    term: Mapped[PolicyTerm] = relationship(back_populates="coverages")


class InsuredItem(Base):
    __tablename__ = "insured_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    item_type: Mapped[str] = mapped_column(Text)
    descriptor: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    term: Mapped[PolicyTerm] = relationship(back_populates="items")


class Document(Base):
    __tablename__ = "document"
    id: Mapped[int] = mapped_column(primary_key=True)
    blob_sha256: Mapped[str] = mapped_column(Text, index=True)
    original_filename: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer)
    has_text_layer: Mapped[bool] = mapped_column(Boolean)
    doc_type: Mapped[str] = mapped_column(Text)  # 'dec_page' in v0
    uploaded_at: Mapped[datetime] = _created_at()


class Extraction(Base):
    __tablename__ = "extraction"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'partial', 'invalid_response', 'failed')",
            name="ck_extraction_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    extractor_version: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    document: Mapped[Document] = relationship()
    fields: Mapped[list["ExtractedField"]] = relationship(back_populates="extraction")


class ExtractedField(Base):
    __tablename__ = "extracted_field"
    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(ForeignKey("extraction.id"))
    field_path: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_text_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction: Mapped[Extraction] = relationship(back_populates="fields")


class Correction(Base):
    """The long-term asset. extracted_field_id is NULL for omissions (the model
    never emitted the field); corrected_value is NULL for hallucinations (the
    value is not on the document)."""

    __tablename__ = "correction"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('wrong_value', 'omission', 'hallucination')",
            name="ck_correction_kind",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(ForeignKey("extraction.id"))
    extracted_field_id: Mapped[int | None] = mapped_column(
        ForeignKey("extracted_field.id"), nullable=True
    )
    field_path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    extracted_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_at: Mapped[datetime] = _created_at()
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RenewalRun(Base):
    """Holds the document pair between upload and promotion, and becomes the
    audit trail of every promotion attempt for that pair."""

    __tablename__ = "renewal_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    prior_document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    renewal_document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    created_at: Mapped[datetime] = _created_at()


class Comparison(Base):
    __tablename__ = "comparison"
    id: Mapped[int] = mapped_column(primary_key=True)
    renewal_run_id: Mapped[int] = mapped_column(ForeignKey("renewal_run.id"))
    prior_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    renewal_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    created_at: Mapped[datetime] = _created_at()
    differences: Mapped[list["Difference"]] = relationship(back_populates="comparison")


class Difference(Base):
    __tablename__ = "difference"
    __table_args__ = (
        CheckConstraint(
            "materiality IN ('material', 'informational', 'noise')",
            name="ck_difference_materiality",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("comparison.id"))
    field_path: Mapped[str] = mapped_column(Text)
    prior_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    renewal_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    materiality: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    comparison: Mapped[Comparison] = relationship(back_populates="differences")


class Reclassification(Base):
    __tablename__ = "reclassification"
    id: Mapped[int] = mapped_column(primary_key=True)
    difference_id: Mapped[int] = mapped_column(ForeignKey("difference.id"))
    from_materiality: Mapped[str] = mapped_column(Text)
    to_materiality: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    reclassified_at: Mapped[datetime] = _created_at()
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Draft(Base):
    """The generated row has final_text and edited_at NULL. An edit inserts a
    new row carrying the same generated_text plus final_text. Latest wins."""

    __tablename__ = "draft"
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("comparison.id"))
    generated_text: Mapped[str] = mapped_column(Text)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

`renewal/db.py`:

```python
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from renewal.config import load_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(load_settings().database_url)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    session = sessionmaker(bind=get_engine())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [x] **Step 5: Generate and apply the migration**

```bash
alembic init migrations
```

In `migrations/env.py`, replace the `target_metadata = None` line with:

```python
from renewal.config import load_settings
from renewal.models import Base

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", load_settings().database_url)
```

Then:

```bash
createdb renewal && createdb renewal_test
alembic revision --autogenerate -m "initial schema"
mv migrations/versions/*_initial_schema.py migrations/versions/0001_initial.py
alembic upgrade head
```

Read the generated migration and confirm it contains all fourteen tables, both
`CheckConstraint`s, and the nullable `coverage.insured_item_id` and
`correction.extracted_field_id` columns.

- [x] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_models.py -v`
Expected: 4 passed

- [x] **Step 7: Commit**

```bash
git add renewal/config.py renewal/db.py renewal/models.py alembic.ini migrations \
        conftest.py tests/test_models.py .env.example PRIVACY.md
git commit -m "feat: insert-only schema, migrations, and settings"
```

---

## Task 3: PDF text and rasterization

**Files:**
- Create: `renewal/pdftext.py`, `tests/pdfmaker.py`
- Test: `tests/test_pdftext.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PageText(page_number: int, text: str)` (1-based); `PdfInfo(page_count: int, has_text_layer: bool, pages: list[PageText])`; `read_pdf(data: bytes) -> PdfInfo`; `layout_text(info: PdfInfo) -> str`; `rasterize(data: bytes, dpi: int = 200) -> list[bytes]`. Test helpers `make_text_pdf(pages: list[list[str]]) -> bytes` and `make_scanned_pdf(pages: list[list[str]]) -> bytes`.

- [x] **Step 1: Write the test PDF helpers**

Real dec pages never enter the repo, so tests build synthetic PDFs.

`tests/pdfmaker.py`:

```python
"""Synthetic PDFs for tests. Never use a real client document here."""

from __future__ import annotations

import fitz


def make_text_pdf(pages: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    return doc.tobytes()


def make_scanned_pdf(pages: list[list[str]]) -> bytes:
    """Same content, rendered to images so there is no text layer."""
    source = fitz.open(stream=make_text_pdf(pages), filetype="pdf")
    out = fitz.open()
    for page in source:
        pix = page.get_pixmap(dpi=150)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    return out.tobytes()
```

- [x] **Step 2: Write the failing tests**

`tests/test_pdftext.py`:

```python
from renewal.pdftext import layout_text, rasterize, read_pdf
from tests.pdfmaker import make_scanned_pdf, make_text_pdf

DEC_LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471   Effective 03/01/2026 to 09/01/2026",
    "Total Policy Premium $1,840.00",
    "Bodily Injury Liability 100/300",
    "2018 Ford F-150 VIN 1FTEW1EP0JKD00001 Collision Deductible $500",
]


def test_read_pdf_reports_page_count_and_one_based_pages():
    info = read_pdf(make_text_pdf([DEC_LINES, ["Page two"]]))
    assert info.page_count == 2
    assert [p.page_number for p in info.pages] == [1, 2]


def test_text_layer_detected_when_text_is_present():
    info = read_pdf(make_text_pdf([DEC_LINES]))
    assert info.has_text_layer is True
    assert "AU-4471" in info.pages[0].text


def test_scanned_pdf_has_no_text_layer():
    info = read_pdf(make_scanned_pdf([DEC_LINES]))
    assert info.has_text_layer is False


def test_layout_text_delimits_pages():
    text = layout_text(read_pdf(make_text_pdf([DEC_LINES, ["Page two"]])))
    assert "=== PAGE 1 ===" in text
    assert "=== PAGE 2 ===" in text
    assert text.index("=== PAGE 1 ===") < text.index("=== PAGE 2 ===")


def test_rasterize_returns_one_png_per_page():
    pngs = rasterize(make_text_pdf([DEC_LINES, ["Page two"]]), dpi=100)
    assert len(pngs) == 2
    assert all(png.startswith(b"\x89PNG") for png in pngs)
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_pdftext.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.pdftext'`

- [x] **Step 4: Write the implementation**

`renewal/pdftext.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import fitz

# A dec page that really has a text layer carries far more than this. Scanned
# pages sometimes carry a stray watermark string, which this threshold ignores.
MIN_CHARS_FOR_TEXT_LAYER = 100


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based, matching what the model is asked to cite
    text: str


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    has_text_layer: bool
    pages: list[PageText]


def read_pdf(data: bytes) -> PdfInfo:
    with fitz.open(stream=data, filetype="pdf") as doc:
        pages = [PageText(i + 1, page.get_text("text")) for i, page in enumerate(doc)]
    has_text_layer = any(
        len("".join(page.text.split())) >= MIN_CHARS_FOR_TEXT_LAYER for page in pages
    )
    return PdfInfo(page_count=len(pages), has_text_layer=has_text_layer, pages=pages)


def layout_text(info: PdfInfo) -> str:
    """Page-delimited text for the prompt, so the model can cite page numbers."""
    return "\n".join(
        f"=== PAGE {page.page_number} ===\n{page.text}" for page in info.pages
    )


def rasterize(data: bytes, dpi: int = 200) -> list[bytes]:
    pngs: list[bytes] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pngs.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    return pngs
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_pdftext.py -v`
Expected: 5 passed

- [x] **Step 6: Commit**

```bash
git add renewal/pdftext.py tests/pdfmaker.py tests/test_pdftext.py
git commit -m "feat: pdf text extraction, text-layer detection, rasterization"
```

---

## Task 4: Ingest and deduplication

**Files:**
- Create: `renewal/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `BlobStore` (Task 1), `Document` (Task 2), `read_pdf` (Task 3).
- Produces: `ingest_pdf(session, store, *, data: bytes, original_filename: str, doc_type: str = "dec_page") -> Document`.

- [x] **Step 1: Write the failing tests**

`tests/test_ingest.py`:

```python
from renewal.ingest import ingest_pdf
from renewal.models import Document
from tests.pdfmaker import make_scanned_pdf, make_text_pdf

LINES = ["PROGRESSIVE PERSONAL AUTO", "Total Policy Premium $1,840.00"] * 8


def test_ingest_records_document_metadata(session, store):
    doc = ingest_pdf(
        session,
        store,
        data=make_text_pdf([LINES, LINES]),
        original_filename="renewal 2026.pdf",
    )
    assert doc.page_count == 2
    assert doc.has_text_layer is True
    assert doc.doc_type == "dec_page"
    assert doc.original_filename == "renewal 2026.pdf"
    assert len(doc.blob_sha256) == 64


def test_identical_bytes_dedup_to_one_blob_but_two_documents(session, store):
    data = make_text_pdf([LINES])
    first = ingest_pdf(session, store, data=data, original_filename="a.pdf")
    second = ingest_pdf(session, store, data=data, original_filename="b.pdf")

    assert first.id != second.id
    assert first.blob_sha256 == second.blob_sha256
    blobs = list(store.root.rglob("*.pdf"))
    assert len(blobs) == 1
    assert session.query(Document).count() == 2


def test_ingest_stores_retrievable_bytes(session, store):
    data = make_text_pdf([LINES])
    doc = ingest_pdf(session, store, data=data, original_filename="a.pdf")
    assert store.get(doc.blob_sha256) == data


def test_scanned_document_is_flagged_without_text_layer(session, store):
    doc = ingest_pdf(
        session, store, data=make_scanned_pdf([LINES]), original_filename="scan.pdf"
    )
    assert doc.has_text_layer is False
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.ingest'`

- [x] **Step 3: Write the implementation**

`renewal/ingest.py`:

```python
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.models import Document
from renewal.pdftext import read_pdf

logger = logging.getLogger(__name__)


def ingest_pdf(
    session: Session,
    store: BlobStore,
    *,
    data: bytes,
    original_filename: str,
    doc_type: str = "dec_page",
) -> Document:
    """Store bytes and record a document row.

    Identical bytes deduplicate to one blob; each upload still gets its own
    document row, because the same PDF arriving twice is two events.
    """
    digest = store.put(data)
    info = read_pdf(data)
    document = Document(
        blob_sha256=digest,
        original_filename=original_filename,
        page_count=info.page_count,
        has_text_layer=info.has_text_layer,
        doc_type=doc_type,
    )
    session.add(document)
    session.flush()
    # Ids and hashes only. Never log document content.
    logger.info(
        "ingested document id=%s sha256=%s pages=%s text_layer=%s",
        document.id,
        digest,
        info.page_count,
        info.has_text_layer,
    )
    return document
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_ingest.py -v`
Expected: 4 passed

- [x] **Step 5: Run the whole suite — build-order step 1 is complete**

Run: `pytest -v`
Expected: all passing. Ingest, dedup, and retrieval are proven.

- [x] **Step 6: Commit**

```bash
git add renewal/ingest.py tests/test_ingest.py
git commit -m "feat: pdf ingest with content-addressed dedup"
```

---
## Task 5: Field-path grammar and the extraction contract

**Files:**
- Create: `renewal/fieldpath.py`, `renewal/extract/__init__.py`, `renewal/extract/schema.py`
- Test: `tests/test_fieldpath.py`, `tests/test_extract_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `fieldpath.is_valid(path: str) -> bool`; `fieldpath.item_key(*, vin, year, make, model) -> str`; `fieldpath.glob_match(pattern: str, path: str) -> bool`; `schema.FieldPayload` (pydantic: `field_path`, `value`, `confidence`, `source_page`, `source_text`); `schema.ExtractionPayload` (`fields: list[FieldPayload]`); `schema.parse_payload(raw: str) -> ExtractionPayload`.

- [x] **Step 1: Write the failing tests for the grammar**

`tests/test_fieldpath.py`:

```python
from renewal.fieldpath import glob_match, is_valid, item_key


def test_policy_paths_are_valid():
    assert is_valid("policy.total_premium")
    assert is_valid("policy.effective_date")


def test_coverage_paths_at_both_levels_are_valid():
    assert is_valid("coverage.BI.limit_value")
    assert is_valid("item.1FTEW1EP0JKD00001.coverage.COLL.deductible_value")


def test_item_paths_are_valid():
    assert is_valid("item.1FTEW1EP0JKD00001.descriptor")
    assert is_valid("item.2019-honda-civic.attributes.garaging_zip")


def test_unknown_paths_are_rejected():
    assert not is_valid("policy.agent_commission")
    assert not is_valid("coverage.BI")
    assert not is_valid("")


def test_item_key_prefers_vin():
    assert (
        item_key(vin="1FTEW1EP0JKD00001", year="2018", make="Ford", model="F-150")
        == "1FTEW1EP0JKD00001"
    )


def test_item_key_falls_back_to_normalized_year_make_model():
    assert item_key(vin=None, year="2019", make="Honda", model="Civic LX") == (
        "2019-honda-civic-lx"
    )


def test_single_star_matches_one_segment_only():
    assert glob_match("coverage.*.deductible_value", "coverage.COLL.deductible_value")
    assert not glob_match(
        "coverage.*.deductible_value",
        "item.VIN1.coverage.COLL.deductible_value",
    )


def test_double_star_matches_any_depth():
    assert glob_match("**.deductible_value", "coverage.COLL.deductible_value")
    assert glob_match(
        "**.deductible_value", "item.VIN1.coverage.COLL.deductible_value"
    )
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_fieldpath.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.fieldpath'`

- [x] **Step 3: Write the grammar module**

`renewal/fieldpath.py`:

```python
"""One canonical field-path grammar, shared by extraction, corrections, the
diff, and the materiality rules. A rule, a correction, and an eval fixture must
all be able to name the same thing the same way.
"""

from __future__ import annotations

import re

_SEG = r"[A-Za-z0-9_-]+"
_COVERAGE_LEAF = r"(limit_value|limit_basis|deductible_value|premium)"

_PATTERNS = [
    re.compile(
        r"^policy\.(carrier_name|policy_number|effective_date|expiration_date"
        r"|total_premium)$"
    ),
    re.compile(rf"^coverage\.{_SEG}\.{_COVERAGE_LEAF}$"),
    re.compile(rf"^item\.{_SEG}\.descriptor$"),
    re.compile(rf"^item\.{_SEG}\.attributes\.{_SEG}$"),
    re.compile(rf"^item\.{_SEG}\.coverage\.{_SEG}\.{_COVERAGE_LEAF}$"),
    re.compile(rf"^forms\.{_SEG}\.edition_date$"),
]


def is_valid(path: str) -> bool:
    return any(pattern.match(path) for pattern in _PATTERNS)


def item_key(
    *,
    vin: str | None,
    year: str | None = None,
    make: str | None = None,
    model: str | None = None,
) -> str:
    """Stable identity for an insured item across terms.

    Full VIN when present; otherwise normalized year-make-model. Items whose key
    appears on only one side of a comparison are an add or a drop.
    """
    if vin:
        return vin.strip().upper()
    parts = [p for p in (year, make, model) if p]
    slug = "-".join(parts).lower()
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def glob_match(pattern: str, path: str) -> bool:
    """`*` matches exactly one segment, `**` matches any number."""
    out = []
    for token in re.split(r"(\*\*|\*)", pattern):
        if token == "**":
            out.append(r".*")
        elif token == "*":
            out.append(r"[^.]+")
        else:
            out.append(re.escape(token))
    return re.match("^" + "".join(out) + "$", path) is not None
```

- [x] **Step 4: Write the failing tests for the response contract**

`tests/test_extract_schema.py`:

```python
import pytest
from pydantic import ValidationError

from renewal.extract.schema import ExtractionPayload, FieldPayload, parse_payload

RAW = """
{"fields": [
  {"field_path": "policy.total_premium", "value": "1840.00", "confidence": 0.96,
   "source_page": 1, "source_text": "Total Policy Premium $1,840.00"}
]}
"""


def test_parse_payload_reads_fields():
    payload = parse_payload(RAW)
    assert isinstance(payload, ExtractionPayload)
    assert payload.fields[0].field_path == "policy.total_premium"
    assert payload.fields[0].confidence == 0.96
    assert payload.fields[0].source_page == 1


def test_parse_payload_tolerates_prose_around_the_json():
    payload = parse_payload("Here you go:\n" + RAW + "\nHope that helps.")
    assert payload.fields[0].value == "1840.00"


def test_confidence_outside_zero_to_one_is_rejected():
    with pytest.raises(ValidationError):
        FieldPayload(
            field_path="policy.total_premium",
            value="1",
            confidence=1.4,
            source_page=1,
            source_text="x",
        )


def test_missing_source_text_is_rejected():
    with pytest.raises(ValidationError):
        FieldPayload(
            field_path="policy.total_premium",
            value="1",
            confidence=0.9,
            source_page=1,
        )


def test_unparseable_response_raises_value_error():
    with pytest.raises(ValueError):
        parse_payload("the document was unreadable")
```

- [x] **Step 5: Run the tests to verify they fail**

Run: `pytest tests/test_extract_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.extract'`

- [x] **Step 6: Write the contract**

`renewal/extract/__init__.py`: empty file.

`renewal/extract/schema.py`:

```python
"""The strict JSON contract the model must answer in.

Every field carries its own provenance. A field without source text cannot be
verified, and an unverifiable field is worthless — so source_text is required
here rather than optional, and a response that omits it fails to parse.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field


class FieldPayload(BaseModel):
    field_path: str
    value: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_page: int = Field(ge=1)
    source_text: str


class ExtractionPayload(BaseModel):
    fields: list[FieldPayload]


def parse_payload(raw: str) -> ExtractionPayload:
    """Parse the model's response, tolerating prose wrapped around the JSON.

    Raises ValueError when no JSON object can be recovered; the caller records
    that as an invalid_response extraction rather than losing the attempt.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return ExtractionPayload.model_validate(json.loads(match.group(0)))
```

- [x] **Step 7: Run both test files to verify they pass**

Run: `pytest tests/test_fieldpath.py tests/test_extract_schema.py -v`
Expected: 13 passed

- [x] **Step 8: Commit**

```bash
git add renewal/fieldpath.py renewal/extract tests/test_fieldpath.py \
        tests/test_extract_schema.py
git commit -m "feat: field-path grammar and strict extraction response contract"
```

---

## Task 6: Source-text validation and confidence gating

**Files:**
- Create: `renewal/extract/validate.py`
- Test: `tests/test_extract_validate.py`

**Interfaces:**
- Consumes: `FieldPayload`, `ExtractionPayload` (Task 5), `PdfInfo` (Task 3), `fieldpath.is_valid` (Task 5).
- Produces: `ValidatedField(payload: FieldPayload, confidence: float, validation_error: str | None, needs_review: bool)`; `validate_fields(payload: ExtractionPayload, pdf: PdfInfo, threshold: float) -> list[ValidatedField]`.

- [x] **Step 1: Write the failing tests**

`tests/test_extract_validate.py`:

```python
from renewal.extract.schema import ExtractionPayload, FieldPayload
from renewal.extract.validate import validate_fields
from renewal.pdftext import read_pdf
from tests.pdfmaker import make_text_pdf

LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Total Policy Premium $1,840.00",
    "Bodily Injury Liability 100/300",
]
PDF = read_pdf(make_text_pdf([LINES]))


def _payload(**overrides):
    base = dict(
        field_path="policy.total_premium",
        value="1840.00",
        confidence=0.96,
        source_page=1,
        source_text="Total Policy Premium $1,840.00",
    )
    base.update(overrides)
    return ExtractionPayload(fields=[FieldPayload(**base)])


def test_field_with_real_source_text_validates():
    result = validate_fields(_payload(), PDF, threshold=0.80)[0]
    assert result.validation_error is None
    assert result.confidence == 0.96
    assert result.needs_review is False


def test_source_text_matching_ignores_whitespace_and_case():
    result = validate_fields(
        _payload(source_text="total policy   premium  $1,840.00"), PDF, threshold=0.80
    )[0]
    assert result.validation_error is None


def test_source_text_absent_from_page_forces_zero_confidence():
    result = validate_fields(
        _payload(source_text="Total Policy Premium $9,999.00"), PDF, threshold=0.80
    )[0]
    assert result.validation_error == "source_text not found on cited page"
    assert result.confidence == 0.0
    assert result.needs_review is True


def test_page_out_of_range_is_a_validation_error():
    result = validate_fields(_payload(source_page=7), PDF, threshold=0.80)[0]
    assert result.validation_error == "source_page out of range"
    assert result.confidence == 0.0


def test_unknown_field_path_is_a_validation_error():
    result = validate_fields(
        _payload(field_path="policy.agent_commission"), PDF, threshold=0.80
    )[0]
    assert result.validation_error == "unknown field_path"
    assert result.confidence == 0.0


def test_low_confidence_field_is_flagged_for_review():
    result = validate_fields(_payload(confidence=0.42), PDF, threshold=0.80)[0]
    assert result.validation_error is None
    assert result.needs_review is True


def test_failed_fields_are_kept_not_dropped():
    payload = ExtractionPayload(
        fields=_payload().fields + _payload(field_path="policy.agent_commission").fields
    )
    assert len(validate_fields(payload, PDF, threshold=0.80)) == 2
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_extract_validate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.extract.validate'`

- [x] **Step 3: Write the implementation**

`renewal/extract/validate.py`:

```python
"""Verify that the model read what it claims to have read.

A field whose source text is not on the page it cites is kept, not discarded:
an unverifiable field is evidence about the extractor, and evidence is the
point of the corpus. It is simply given zero confidence, which sends it to
review and keeps it out of the draft.
"""

from __future__ import annotations

from dataclasses import dataclass

from renewal import fieldpath
from renewal.extract.schema import ExtractionPayload, FieldPayload
from renewal.pdftext import PdfInfo


@dataclass(frozen=True)
class ValidatedField:
    payload: FieldPayload
    confidence: float
    validation_error: str | None
    needs_review: bool


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _check(field: FieldPayload, pdf: PdfInfo) -> str | None:
    if not fieldpath.is_valid(field.field_path):
        return "unknown field_path"
    if not 1 <= field.source_page <= pdf.page_count:
        return "source_page out of range"
    page = pdf.pages[field.source_page - 1]
    if _normalize(field.source_text) not in _normalize(page.text):
        return "source_text not found on cited page"
    return None


def validate_fields(
    payload: ExtractionPayload, pdf: PdfInfo, threshold: float
) -> list[ValidatedField]:
    results = []
    for field in payload.fields:
        error = _check(field, pdf)
        confidence = 0.0 if error else field.confidence
        results.append(
            ValidatedField(
                payload=field,
                confidence=confidence,
                validation_error=error,
                needs_review=confidence < threshold,
            )
        )
    return results
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_extract_validate.py -v`
Expected: 7 passed

- [x] **Step 5: Commit**

```bash
git add renewal/extract/validate.py tests/test_extract_validate.py
git commit -m "feat: source-text validation and confidence gating"
```

---

## Task 7: Extractor runner and prompt v1

**Files:**
- Create: `renewal/extract/prompt_v1.py`, `renewal/extract/runner.py`
- Test: `tests/test_extract_runner.py`

**Interfaces:**
- Consumes: `BlobStore`, `Document`, `Extraction`, `ExtractedField`, `read_pdf`, `layout_text`, `parse_payload`, `validate_fields`, `Settings`.
- Produces: `ModelClient` protocol with `complete(*, model: str, system: str, content: list[dict]) -> str`; `AnthropicClient(api_key: str)`; `PROMPTS: dict[str, str]` keyed by extractor version; `extract(session, store, document, version, *, client, settings) -> Extraction`.

The spec's signature is `extract(document, version)`. It is widened here to inject
the session, blob store, and model client, which is what makes it testable without
network access. Purity with respect to the document is preserved: same document,
same version, same prompt, temperature 0.

- [x] **Step 1: Write the frozen prompt**

`renewal/extract/prompt_v1.py`:

```python
"""Extractor version v1. Frozen once shipped.

Changing this text means adding v2, not editing v1 — otherwise
extractor_version stops meaning anything and version comparison in the eval
harness becomes worthless.
"""

VERSION = "v1"

SYSTEM = """You extract structured data from a US personal auto insurance
declarations page. You return JSON only.

Return an object with one key, "fields", whose value is a list. Each entry has:
  field_path   one of the paths listed below, exactly
  value        the value as written on the document, as a string
  confidence   0.0 to 1.0, your honest confidence in this single value
  source_page  the 1-based page number you read it from
  source_text  the verbatim text from that page that you read it from

source_text must appear on the cited page character for character, apart from
whitespace. It is checked. If you cannot quote the document for a value, give
that field a confidence at or below 0.3.

Allowed field paths:
  policy.carrier_name
  policy.policy_number
  policy.effective_date
  policy.expiration_date
  policy.total_premium
  coverage.<CODE>.limit_value          policy-level coverage
  coverage.<CODE>.limit_basis
  coverage.<CODE>.deductible_value
  coverage.<CODE>.premium
  item.<KEY>.descriptor
  item.<KEY>.attributes.<name>
  item.<KEY>.coverage.<CODE>.limit_value      coverage on one vehicle
  item.<KEY>.coverage.<CODE>.limit_basis
  item.<KEY>.coverage.<CODE>.deductible_value
  item.<KEY>.coverage.<CODE>.premium
  forms.<FORM_NUMBER>.edition_date

<CODE> is the carrier's coverage abbreviation, uppercased: BI, PD, UM, UIM,
MED, COMP, COLL, RENT, TOW.
<KEY> is the vehicle's full VIN when the document shows one. When it does not,
use lowercase year-make-model joined by hyphens, e.g. 2019-honda-civic.

Liability and UM/UIM coverages are policy-level: use coverage.<CODE>.
Comprehensive and collision belong to a specific vehicle: use
item.<KEY>.coverage.<CODE>, so that each vehicle's own deductible and premium
are preserved.

Dates as YYYY-MM-DD. Money as digits with a decimal point and no currency
symbol or thousands separator: 1840.00. Limits as written: 100/300.

Extract only what is on the document. Do not infer, do not compute, do not fill
in what a policy of this kind usually contains.
"""

USER_TEXT_TEMPLATE = """Extract the declarations page below.

{document_text}
"""

USER_IMAGE_INSTRUCTION = (
    "Extract the declarations page in the attached page images. Page 1 is the "
    "first image, page 2 the second, and so on."
)

PROMPTS = {VERSION: SYSTEM}
```

- [x] **Step 2: Write the failing tests**

`tests/test_extract_runner.py`:

```python
import json
import logging

import pytest

from renewal.config import Settings
from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from renewal.models import ExtractedField
from tests.pdfmaker import make_text_pdf

LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $1,840.00",
    "Bodily Injury Liability 100/300",
]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path,
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )


class FakeClient:
    """Stands in for the Anthropic API. Records what it was asked."""

    def __init__(self, response, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append({"model": model, "system": system, "content": content})
        if self.raises:
            raise self.raises
        return self.response


def _good_response():
    return json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "1840.00",
                    "confidence": 0.96,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $1,840.00",
                },
                {
                    "field_path": "coverage.BI.limit_value",
                    "value": "100/300",
                    "confidence": 0.55,
                    "source_page": 1,
                    "source_text": "Bodily Injury Liability 100/300",
                },
            ]
        }
    )


def _doc(session, store):
    return ingest_pdf(
        session, store, data=make_text_pdf([LINES]), original_filename="dec.pdf"
    )


def test_extraction_persists_fields_with_provenance(session, store, settings):
    document = _doc(session, store)
    client = FakeClient(_good_response())

    extraction = extract(
        session, store, document, "v1", client=client, settings=settings
    )

    assert extraction.status == "ok"
    assert extraction.extractor_version == "v1"
    assert extraction.model_id == "claude-opus-5"
    fields = {f.field_path: f for f in extraction.fields}
    assert fields["policy.total_premium"].value == "1840.00"
    assert fields["policy.total_premium"].source_page == 1
    assert (
        fields["policy.total_premium"].source_text_span
        == "Total Policy Premium $1,840.00"
    )


def test_low_confidence_field_is_flagged_needs_review(session, store, settings):
    document = _doc(session, store)
    extraction = extract(
        session, store, document, "v1", client=FakeClient(_good_response()),
        settings=settings,
    )
    fields = {f.field_path: f for f in extraction.fields}
    assert fields["coverage.BI.limit_value"].needs_review is True
    assert fields["policy.total_premium"].needs_review is False


def test_unverifiable_source_text_yields_partial_status(session, store, settings):
    document = _doc(session, store)
    response = json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "9999.00",
                    "confidence": 0.99,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $9,999.00",
                }
            ]
        }
    )
    extraction = extract(
        session, store, document, "v1", client=FakeClient(response), settings=settings
    )
    assert extraction.status == "partial"
    field = extraction.fields[0]
    assert field.validation_error == "source_text not found on cited page"
    assert field.confidence == 0.0
    assert field.value == "9999.00"


def test_unparseable_response_is_recorded_not_lost(session, store, settings):
    document = _doc(session, store)
    extraction = extract(
        session, store, document, "v1", client=FakeClient("could not read it"),
        settings=settings,
    )
    assert extraction.status == "invalid_response"
    assert extraction.raw_response["text"] == "could not read it"
    assert extraction.fields == []


def test_api_failure_is_recorded_not_lost(session, store, settings):
    document = _doc(session, store)
    client = FakeClient(None, raises=RuntimeError("connection reset"))
    extraction = extract(
        session, store, document, "v1", client=client, settings=settings
    )
    assert extraction.status == "failed"
    assert "connection reset" in extraction.raw_response["error"]


def test_retry_creates_a_new_extraction_and_leaves_the_old_one(
    session, store, settings
):
    document = _doc(session, store)
    first = extract(
        session, store, document, "v1", client=FakeClient("garbage"), settings=settings
    )
    second = extract(
        session, store, document, "v1", client=FakeClient(_good_response()),
        settings=settings,
    )
    assert first.id != second.id
    assert first.status == "invalid_response"
    assert second.status == "ok"


def test_text_path_sends_document_text_not_images(session, store, settings):
    document = _doc(session, store)
    client = FakeClient(_good_response())
    extract(session, store, document, "v1", client=client, settings=settings)
    content = client.calls[0]["content"]
    assert content[0]["type"] == "text"
    assert "AU-4471" in content[0]["text"]


def test_no_extracted_content_reaches_the_logs(session, store, settings, caplog):
    document = _doc(session, store)
    with caplog.at_level(logging.DEBUG):
        extract(
            session, store, document, "v1", client=FakeClient(_good_response()),
            settings=settings,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "1840.00" not in logged
    assert "AU-4471" not in logged
    assert "100/300" not in logged
    assert str(document.id) in logged
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_extract_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.extract.runner'`

- [x] **Step 4: Write the implementation**

`renewal/extract/runner.py`:

```python
"""Extraction: a versioned, re-runnable function of a document.

Nothing here mutates an earlier extraction. A retry, a prompt change, or a new
model all produce new extraction rows, so any version can be re-run over the
whole corpus and the results compared.
"""

from __future__ import annotations

import base64
import logging
from typing import Protocol

from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.extract import prompt_v1
from renewal.extract.schema import parse_payload
from renewal.extract.validate import validate_fields
from renewal.models import Document, ExtractedField, Extraction
from renewal.pdftext import layout_text, rasterize, read_pdf

logger = logging.getLogger(__name__)

PROMPTS = {prompt_v1.VERSION: prompt_v1}


class ModelClient(Protocol):
    def complete(self, *, model: str, system: str, content: list[dict]) -> str: ...


class AnthropicClient:
    def __init__(self, api_key: str) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, *, model: str, system: str, content: list[dict]) -> str:
        message = self._client.messages.create(
            model=model,
            max_tokens=8192,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(block.text for block in message.content if block.type == "text")


def _build_content(prompt, data: bytes, has_text_layer: bool) -> list[dict]:
    if has_text_layer:
        text = prompt.USER_TEXT_TEMPLATE.format(document_text=layout_text(read_pdf(data)))
        return [{"type": "text", "text": text}]
    blocks: list[dict] = [{"type": "text", "text": prompt.USER_IMAGE_INSTRUCTION}]
    for png in rasterize(data):
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(png).decode(),
                },
            }
        )
    return blocks


def extract(
    session: Session,
    store: BlobStore,
    document: Document,
    version: str,
    *,
    client: ModelClient,
    settings: Settings,
) -> Extraction:
    prompt = PROMPTS[version]
    data = store.get(document.blob_sha256)
    content = _build_content(prompt, data, document.has_text_layer)

    def _record(status: str, raw: dict | None) -> Extraction:
        extraction = Extraction(
            document_id=document.id,
            extractor_version=version,
            model_id=settings.extraction_model,
            raw_response=raw,
            status=status,
        )
        session.add(extraction)
        session.flush()
        # Ids, hashes, counts, status. Never field values or document text.
        logger.info(
            "extraction id=%s document_id=%s sha256=%s version=%s status=%s",
            extraction.id,
            document.id,
            document.blob_sha256,
            version,
            status,
        )
        return extraction

    try:
        raw_text = client.complete(
            model=settings.extraction_model, system=prompt.SYSTEM, content=content
        )
    except Exception as exc:  # noqa: BLE001 - the failure itself is the record
        return _record("failed", {"error": str(exc)})

    try:
        payload = parse_payload(raw_text)
    except Exception:  # noqa: BLE001 - unparseable text is kept verbatim
        return _record("invalid_response", {"text": raw_text})

    validated = validate_fields(
        payload, read_pdf(data), settings.confidence_threshold
    )
    status = "partial" if any(v.validation_error for v in validated) else "ok"
    extraction = _record(status, {"text": raw_text})
    for item in validated:
        session.add(
            ExtractedField(
                extraction_id=extraction.id,
                field_path=item.payload.field_path,
                value=item.payload.value,
                confidence=item.confidence,
                source_page=item.payload.source_page,
                source_text_span=item.payload.source_text,
                validation_error=item.validation_error,
                needs_review=item.needs_review,
            )
        )
    session.flush()
    session.refresh(extraction)
    return extraction
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_extract_runner.py -v`
Expected: 8 passed

- [x] **Step 6: Run the whole suite — build-order step 2 is complete**

Run: `pytest -v`
Expected: all passing. Text-layer extraction with provenance and confidence works.

- [x] **Step 7: Commit**

```bash
git add renewal/extract/prompt_v1.py renewal/extract/runner.py \
        tests/test_extract_runner.py
git commit -m "feat: versioned extractor runner with provenance and failure records"
```

---

## Task 8: Corrections

**Files:**
- Create: `renewal/corrections.py`, `scripts/export_corrections.py`
- Test: `tests/test_corrections.py`

**Interfaces:**
- Consumes: `Correction`, `ExtractedField`, `Extraction` (Task 2).
- Produces: `record_correction(session, *, extraction_id, field_path, kind, extracted_field_id=None, extracted_value=None, corrected_value=None, note=None) -> Correction`; `effective_values(session, extraction_id) -> dict[str, str | None]`; `export_rows(session) -> list[dict]`.

- [x] **Step 1: Write the failing tests**

`tests/test_corrections.py`:

```python
import json

import pytest

from renewal.corrections import effective_values, export_rows, record_correction
from renewal.models import Correction, Document, ExtractedField, Extraction


@pytest.fixture
def extraction(session):
    document = Document(
        blob_sha256="d" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    extraction = Extraction(
        document_id=document.id,
        extractor_version="v1",
        model_id="claude-opus-5",
        status="ok",
    )
    session.add(extraction)
    session.flush()
    session.add_all(
        [
            ExtractedField(
                extraction_id=extraction.id,
                field_path="policy.total_premium",
                value="1840.00",
                confidence=0.96,
                source_page=1,
                source_text_span="Total Policy Premium $1,840.00",
            ),
            ExtractedField(
                extraction_id=extraction.id,
                field_path="coverage.BI.limit_value",
                value="100/300",
                confidence=0.91,
                source_page=1,
                source_text_span="Bodily Injury Liability 100/300",
            ),
        ]
    )
    session.flush()
    return extraction


def _field(session, extraction, path):
    return (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path=path)
        .one()
    )


def test_uncorrected_extraction_returns_extracted_values(session, extraction):
    assert effective_values(session, extraction.id) == {
        "policy.total_premium": "1840.00",
        "coverage.BI.limit_value": "100/300",
    }


def test_wrong_value_correction_replaces_without_touching_the_field(
    session, extraction
):
    field = _field(session, extraction, "policy.total_premium")
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value=field.value,
        corrected_value="1804.00",
    )
    assert effective_values(session, extraction.id)["policy.total_premium"] == "1804.00"
    session.refresh(field)
    assert field.value == "1840.00"  # the extracted row is never edited


def test_omission_adds_a_field_the_model_never_emitted(session, extraction):
    record_correction(
        session,
        extraction_id=extraction.id,
        field_path="coverage.UMBI.limit_value",
        kind="omission",
        corrected_value="100/300",
    )
    values = effective_values(session, extraction.id)
    assert values["coverage.UMBI.limit_value"] == "100/300"


def test_hallucination_removes_a_field_that_is_not_on_the_document(
    session, extraction
):
    field = _field(session, extraction, "coverage.BI.limit_value")
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="hallucination",
        extracted_value=field.value,
    )
    assert "coverage.BI.limit_value" not in effective_values(session, extraction.id)


def test_latest_correction_wins_and_earlier_ones_are_kept(session, extraction):
    field = _field(session, extraction, "policy.total_premium")
    for value in ("1804.00", "1840.50"):
        record_correction(
            session,
            extraction_id=extraction.id,
            extracted_field_id=field.id,
            field_path=field.field_path,
            kind="wrong_value",
            extracted_value=field.value,
            corrected_value=value,
        )
    assert effective_values(session, extraction.id)["policy.total_premium"] == "1840.50"
    assert session.query(Correction).count() == 2


def test_export_rows_emit_all_three_kinds_with_the_extractor_version(
    session, extraction
):
    field = _field(session, extraction, "policy.total_premium")
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value="1840.00",
        corrected_value="1804.00",
    )
    record_correction(
        session,
        extraction_id=extraction.id,
        field_path="coverage.UMBI.limit_value",
        kind="omission",
        corrected_value="100/300",
    )
    rows = export_rows(session)
    assert {row["kind"] for row in rows} == {"wrong_value", "omission"}
    assert all(row["extractor_version"] == "v1" for row in rows)
    assert all(row["blob_sha256"] == "d" * 64 for row in rows)
    json.dumps(rows)  # must be serializable as-is
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_corrections.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.corrections'`

- [x] **Step 3: Write the implementation**

`renewal/corrections.py`:

```python
"""Corrections are the long-term asset of this project.

Nothing here edits an extracted field. A correction is a new row that says what
the model produced, what the truth was, and which extractor version was
responsible. That record is what turns real usage into a labeled evaluation
set.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from renewal.models import Correction, Document, ExtractedField, Extraction

KINDS = ("wrong_value", "omission", "hallucination")


def record_correction(
    session: Session,
    *,
    extraction_id: int,
    field_path: str,
    kind: str,
    extracted_field_id: int | None = None,
    extracted_value: str | None = None,
    corrected_value: str | None = None,
    note: str | None = None,
) -> Correction:
    if kind not in KINDS:
        raise ValueError(f"unknown correction kind: {kind}")
    correction = Correction(
        extraction_id=extraction_id,
        extracted_field_id=extracted_field_id,
        field_path=field_path,
        kind=kind,
        extracted_value=extracted_value,
        corrected_value=corrected_value,
        note=note,
    )
    session.add(correction)
    session.flush()
    return correction


def effective_values(session: Session, extraction_id: int) -> dict[str, str | None]:
    """What this extraction now says, after applying every correction on it.

    Later corrections override earlier ones for the same path.
    """
    values: dict[str, str | None] = {
        field.field_path: field.value
        for field in session.query(ExtractedField)
        .filter_by(extraction_id=extraction_id)
        .order_by(ExtractedField.id)
    }
    corrections = (
        session.query(Correction)
        .filter_by(extraction_id=extraction_id)
        .order_by(Correction.id)
    )
    for correction in corrections:
        if correction.kind == "hallucination":
            values.pop(correction.field_path, None)
        else:
            values[correction.field_path] = correction.corrected_value
    return values


def export_rows(session: Session) -> list[dict]:
    """Corrections as a labeled dataset, one row per correction."""
    query = (
        session.query(Correction, Extraction, Document)
        .join(Extraction, Correction.extraction_id == Extraction.id)
        .join(Document, Extraction.document_id == Document.id)
        .order_by(Correction.id)
    )
    return [
        {
            "correction_id": correction.id,
            "blob_sha256": document.blob_sha256,
            "extractor_version": extraction.extractor_version,
            "model_id": extraction.model_id,
            "field_path": correction.field_path,
            "kind": correction.kind,
            "extracted_value": correction.extracted_value,
            "corrected_value": correction.corrected_value,
            "note": correction.note,
            "corrected_at": correction.corrected_at.isoformat(),
        }
        for correction, extraction, document in query
    ]
```

`scripts/export_corrections.py`:

```python
"""Dump corrections as a labeled dataset (JSON Lines on stdout).

Usage: python scripts/export_corrections.py > corrections.jsonl

The output contains extracted client data by design — it is training data.
Treat the file the same way the blob store is treated: never commit it.
"""

from __future__ import annotations

import json
import sys

from renewal.corrections import export_rows
from renewal.db import session_scope


def main() -> None:
    with session_scope() as session:
        for row in export_rows(session):
            sys.stdout.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_corrections.py -v`
Expected: 6 passed

- [x] **Step 5: Commit**

```bash
git add renewal/corrections.py scripts/export_corrections.py tests/test_corrections.py
git commit -m "feat: corrections with omission and hallucination kinds, plus export"
```

---
## Task 9: Review UI — upload, extract, correct

**Files:**
- Create: `renewal/web.py`, `renewal/templates/base.html`, `renewal/templates/index.html`, `renewal/templates/run_new.html`, `renewal/templates/run_review.html`, `renewal/static/app.js`
- Test: `tests/test_web_review.py`

**Interfaces:**
- Consumes: `ingest_pdf`, `extract`, `record_correction`, `effective_values`, models, `Settings`, `BlobStore`.
- Produces: `create_app(*, settings, store, model_client, session_factory) -> FastAPI`.

- [x] **Step 1: Write the failing tests**

`tests/test_web_review.py`:

```python
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.config import Settings
from renewal.models import Client, Correction, Document, Extraction, Policy, RenewalRun
from renewal.web import create_app
from tests.pdfmaker import make_text_pdf

PRIOR = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $1,840.00",
]
RENEWAL = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $2,180.00",
]


class FakeClient:
    def __init__(self, response):
        self.response = response

    def complete(self, *, model, system, content):
        return self.response


def _response(premium, source):
    return json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": premium,
                    "confidence": 0.35,
                    "source_page": 1,
                    "source_text": source,
                }
            ]
        }
    )


@pytest.fixture
def app(engine, clean_db, tmp_path):
    from renewal.blobstore import BlobStore

    settings = Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=FakeClient(_response("1840.00", "Total Policy Premium $1,840.00")),
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def seeded(engine):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Ramirez Landscaping")
    sess.add(client)
    sess.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    sess.add(policy)
    sess.commit()
    ids = (client.id, policy.id)
    sess.close()
    yield ids


def _upload(client, policy_id):
    return client.post(
        "/runs",
        data={"policy_id": str(policy_id)},
        files={
            "prior": ("prior.pdf", make_text_pdf([PRIOR]), "application/pdf"),
            "renewal": ("renewal.pdf", make_text_pdf([RENEWAL]), "application/pdf"),
        },
        follow_redirects=False,
    )


def test_new_run_form_lists_policies(app, seeded):
    with TestClient(app) as client:
        page = client.get("/runs/new")
    assert page.status_code == 200
    assert "Ramirez Landscaping" in page.text


def test_upload_ingests_both_documents_and_extracts_each(app, seeded, engine):
    _, policy_id = seeded
    with TestClient(app) as client:
        response = _upload(client, policy_id)
    assert response.status_code == 303
    assert "/review" in response.headers["location"]

    sess = sessionmaker(bind=engine)()
    assert sess.query(RenewalRun).count() == 1
    assert sess.query(Document).count() == 2
    assert sess.query(Extraction).count() == 2
    sess.close()


def test_review_page_shows_value_confidence_and_source(app, seeded):
    _, policy_id = seeded
    with TestClient(app) as client:
        location = _upload(client, policy_id).headers["location"]
        page = client.get(location)
    assert "policy.total_premium" in page.text
    assert "1840.00" in page.text
    assert "Total Policy Premium $1,840.00" in page.text
    assert "page 1" in page.text
    assert "needs review" in page.text  # confidence 0.35 < 0.80


def test_correcting_a_field_writes_a_correction_and_leaves_the_field(
    app, seeded, engine
):
    _, policy_id = seeded
    with TestClient(app) as client:
        location = _upload(client, policy_id).headers["location"]
        sess = sessionmaker(bind=engine)()
        field_id = sess.query(Extraction).first().fields[0].id
        sess.close()

        response = client.post(
            f"/fields/{field_id}/correct", data={"corrected_value": "1804.00"}
        )
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    correction = sess.query(Correction).one()
    assert correction.kind == "wrong_value"
    assert correction.extracted_value == "1840.00"
    assert correction.corrected_value == "1804.00"
    assert correction.extracted_field_id == field_id
    sess.close()


def test_adding_a_missing_field_records_an_omission(app, seeded, engine):
    _, policy_id = seeded
    with TestClient(app) as client:
        _upload(client, policy_id)
        sess = sessionmaker(bind=engine)()
        extraction_id = sess.query(Extraction).first().id
        sess.close()

        response = client.post(
            f"/extractions/{extraction_id}/fields",
            data={
                "field_path": "coverage.UMBI.limit_value",
                "corrected_value": "100/300",
            },
        )
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    correction = sess.query(Correction).one()
    assert correction.kind == "omission"
    assert correction.extracted_field_id is None
    sess.close()


def test_rejecting_a_field_records_a_hallucination(app, seeded, engine):
    _, policy_id = seeded
    with TestClient(app) as client:
        _upload(client, policy_id)
        sess = sessionmaker(bind=engine)()
        field_id = sess.query(Extraction).first().fields[0].id
        sess.close()

        response = client.post(f"/fields/{field_id}/reject")
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    assert sess.query(Correction).one().kind == "hallucination"
    sess.close()


def test_identical_documents_in_both_slots_are_refused_without_confirmation(
    app, seeded
):
    _, policy_id = seeded
    same = make_text_pdf([PRIOR])
    with TestClient(app) as client:
        response = client.post(
            "/runs",
            data={"policy_id": str(policy_id)},
            files={
                "prior": ("a.pdf", same, "application/pdf"),
                "renewal": ("b.pdf", same, "application/pdf"),
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "same document" in response.text
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_web_review.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.web'`

- [x] **Step 3: Write the templates**

`renewal/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{% block title %}Renewal comparison{% endblock %}</title>
  <style>
    body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 70rem;
           color: #111; }
    h1, h2 { font-weight: 600; }
    table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
    th, td { border-bottom: 1px solid #ddd; padding: .4rem .6rem; text-align: left;
             vertical-align: top; }
    th { background: #f4f4f4; font-weight: 600; }
    .source { color: #555; font-size: 13px; }
    .flag { color: #a00; font-weight: 600; }
    .saved { color: #070; }
    input[type=text] { font: inherit; padding: .2rem .3rem; width: 14rem; }
    .cols { display: flex; gap: 2rem; }
    .cols > * { flex: 1; min-width: 0; }
    form.inline { display: inline; }
  </style>
</head>
<body>
  <p><a href="/">Renewal comparison</a></p>
  {% block content %}{% endblock %}
  <script src="/static/app.js"></script>
</body>
</html>
```

`renewal/templates/index.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Renewal runs</h1>
<p><a href="/runs/new">New renewal</a></p>
<table>
  <tr><th>Run</th><th>Client</th><th>Policy</th><th>Uploaded</th></tr>
  {% for run, client, policy in runs %}
  <tr>
    <td><a href="/runs/{{ run.id }}/review">#{{ run.id }}</a></td>
    <td>{{ client.display_name }}</td>
    <td>{{ policy.carrier_name }} {{ policy.policy_number }}</td>
    <td>{{ run.created_at.strftime("%Y-%m-%d %H:%M") }}</td>
  </tr>
  {% endfor %}
</table>
{% endblock %}
```

`renewal/templates/run_new.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>New renewal</h1>
{% if error %}<p class="flag">{{ error }}</p>{% endif %}
<form method="post" action="/runs" enctype="multipart/form-data">
  <p>
    <label>Policy
      <select name="policy_id" required>
        {% for client, policy in policies %}
        <option value="{{ policy.id }}">
          {{ client.display_name }} — {{ policy.carrier_name }}
          {{ policy.policy_number }} ({{ policy.line_of_business }})
        </option>
        {% endfor %}
      </select>
    </label>
  </p>
  <p><label>Prior term PDF <input type="file" name="prior" accept="application/pdf"
       required></label></p>
  <p><label>Renewal term PDF <input type="file" name="renewal"
       accept="application/pdf" required></label></p>
  <p><label><input type="checkbox" name="confirm_same" value="1">
     These really are the same document</label></p>
  <p><button type="submit">Ingest and extract</button></p>
</form>

<h2>Add a client</h2>
<form method="post" action="/clients">
  <input type="text" name="display_name" placeholder="Client name" required>
  <button type="submit">Add client</button>
</form>

<h2>Add a policy</h2>
<form method="post" action="/policies">
  <select name="client_id" required>
    {% for client in clients %}
    <option value="{{ client.id }}">{{ client.display_name }}</option>
    {% endfor %}
  </select>
  <input type="text" name="carrier_name" placeholder="Carrier" required>
  <input type="text" name="policy_number" placeholder="Policy number" required>
  <input type="text" name="line_of_business" value="personal_auto" required>
  <button type="submit">Add policy</button>
</form>
{% endblock %}
```

`renewal/templates/run_review.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Run #{{ run.id }} — review</h1>
<p>{{ client.display_name }} — {{ policy.carrier_name }} {{ policy.policy_number }}</p>

<div class="cols">
  {% for side, extraction, fields in sides %}
  <div>
    <h2>{{ side }} term</h2>
    <p class="source">
      extraction #{{ extraction.id }} · {{ extraction.extractor_version }} ·
      {{ extraction.model_id }} · status {{ extraction.status }}
    </p>
    <table>
      <tr><th>Field</th><th>Value</th><th>Source</th></tr>
      {% for field in fields %}
      <tr>
        <td>{{ field.field_path }}</td>
        <td>
          <input type="text" value="{{ field.effective_value or '' }}"
                 data-field-id="{{ field.id }}"
                 data-original="{{ field.value or '' }}"
                 class="correctable">
          <form class="inline" method="post" action="/fields/{{ field.id }}/reject">
            <button type="submit" title="Not on the document">not present</button>
          </form>
          <span class="saved" data-saved-for="{{ field.id }}"></span>
        </td>
        <td class="source">
          {% if field.needs_review %}<span class="flag">needs review</span>{% endif %}
          confidence {{ "%.2f"|format(field.confidence) }} · page {{ field.source_page }}
          {% if field.validation_error %}
            <br><span class="flag">{{ field.validation_error }}</span>
          {% endif %}
          <br>{{ field.source_text_span }}
        </td>
      </tr>
      {% endfor %}
      {% for path, value in extra_fields[extraction.id] %}
      <tr>
        <td>{{ path }}</td>
        <td>{{ value }}</td>
        <td class="source">added by hand</td>
      </tr>
      {% endfor %}
    </table>

    <form method="post" action="/extractions/{{ extraction.id }}/fields"
          class="add-missing">
      <input type="text" name="field_path" placeholder="field path" required>
      <input type="text" name="corrected_value" placeholder="value" required>
      <button type="submit">Add missing field</button>
    </form>
  </div>
  {% endfor %}
</div>
{% endblock %}
```

`renewal/static/app.js`:

```javascript
// Correcting a field is one click, typing, and Enter. No save button:
// friction here destroys the corrections dataset.
document.querySelectorAll("input.correctable").forEach(function (input) {
  function save() {
    if (input.value === input.dataset.saved) return;
    if (input.value === input.dataset.original) return;
    var body = new FormData();
    body.append("corrected_value", input.value);
    fetch("/fields/" + input.dataset.fieldId + "/correct", {
      method: "POST",
      body: body,
    }).then(function (response) {
      var flag = document.querySelector(
        '[data-saved-for="' + input.dataset.fieldId + '"]'
      );
      if (response.ok) {
        input.dataset.saved = input.value;
        flag.textContent = "saved";
      } else {
        flag.textContent = "save failed";
      }
    });
  }
  input.addEventListener("blur", save);
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      save();
      input.blur();
    }
  });
});

// The add-missing and reject forms post normally but must not navigate away.
document.querySelectorAll("form.inline, form.add-missing").forEach(function (form) {
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    fetch(form.action, { method: "POST", body: new FormData(form) }).then(function () {
      window.location.reload();
    });
  });
});
```

- [x] **Step 4: Write the application**

`renewal/web.py`:

```python
"""FastAPI application. Routes parse the request and call the library; no
pipeline logic lives here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.corrections import effective_values, record_correction
from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from renewal.models import (
    Client,
    Document,
    ExtractedField,
    Extraction,
    Policy,
    RenewalRun,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(*, settings: Settings, store: BlobStore, model_client, session_factory):
    app = FastAPI()
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    def db() -> Session:
        return session_factory()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        session = db()
        runs = (
            session.query(RenewalRun, Client, Policy)
            .join(Policy, RenewalRun.policy_id == Policy.id)
            .join(Client, Policy.client_id == Client.id)
            .order_by(RenewalRun.id.desc())
            .all()
        )
        page = TEMPLATES.TemplateResponse(
            request, "index.html", {"runs": runs}
        )
        session.close()
        return page

    @app.get("/runs/new", response_class=HTMLResponse)
    def new_run(request: Request, error: str | None = None):
        session = db()
        policies = (
            session.query(Client, Policy)
            .join(Policy, Policy.client_id == Client.id)
            .order_by(Client.display_name)
            .all()
        )
        clients = session.query(Client).order_by(Client.display_name).all()
        page = TEMPLATES.TemplateResponse(
            request,
            "run_new.html",
            {"policies": policies, "clients": clients, "error": error},
        )
        session.close()
        return page

    @app.post("/clients")
    def add_client(display_name: str = Form(...)):
        session = db()
        session.add(Client(display_name=display_name))
        session.commit()
        session.close()
        return RedirectResponse("/runs/new", status_code=303)

    @app.post("/policies")
    def add_policy(
        client_id: int = Form(...),
        carrier_name: str = Form(...),
        policy_number: str = Form(...),
        line_of_business: str = Form(...),
    ):
        session = db()
        session.add(
            Policy(
                client_id=client_id,
                carrier_name=carrier_name,
                policy_number=policy_number,
                line_of_business=line_of_business,
            )
        )
        session.commit()
        session.close()
        return RedirectResponse("/runs/new", status_code=303)

    @app.post("/runs")
    async def create_run(
        policy_id: int = Form(...),
        prior: UploadFile = ...,
        renewal: UploadFile = ...,
        confirm_same: str | None = Form(None),
    ):
        prior_bytes = await prior.read()
        renewal_bytes = await renewal.read()
        same = hashlib.sha256(prior_bytes).digest() == hashlib.sha256(
            renewal_bytes
        ).digest()
        if same and not confirm_same:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Both slots hold the same document. Tick the confirmation box "
                    "if that is deliberate."
                ),
            )

        session = db()
        prior_doc = ingest_pdf(
            session, store, data=prior_bytes, original_filename=prior.filename
        )
        renewal_doc = ingest_pdf(
            session, store, data=renewal_bytes, original_filename=renewal.filename
        )
        run = RenewalRun(
            policy_id=policy_id,
            prior_document_id=prior_doc.id,
            renewal_document_id=renewal_doc.id,
        )
        session.add(run)
        session.flush()
        for document in (prior_doc, renewal_doc):
            extract(
                session,
                store,
                document,
                "v1",
                client=model_client,
                settings=settings,
            )
        session.commit()
        run_id = run.id
        session.close()
        return RedirectResponse(f"/runs/{run_id}/review", status_code=303)

    def _latest_extraction(session: Session, document_id: int) -> Extraction:
        return (
            session.query(Extraction)
            .filter_by(document_id=document_id)
            .order_by(Extraction.id.desc())
            .first()
        )

    @app.get("/runs/{run_id}/review", response_class=HTMLResponse)
    def review(request: Request, run_id: int):
        session = db()
        run = session.get(RenewalRun, run_id)
        if run is None:
            session.close()
            raise HTTPException(status_code=404, detail="no such run")
        policy = session.get(Policy, run.policy_id)
        client = session.get(Client, policy.client_id)

        sides, extra_fields = [], {}
        for label, document_id in (
            ("Prior", run.prior_document_id),
            ("Renewal", run.renewal_document_id),
        ):
            extraction = _latest_extraction(session, document_id)
            values = effective_values(session, extraction.id)
            fields = (
                session.query(ExtractedField)
                .filter_by(extraction_id=extraction.id)
                .order_by(ExtractedField.field_path)
                .all()
            )
            for field in fields:
                field.effective_value = values.get(field.field_path, field.value)
            emitted = {field.field_path for field in fields}
            extra_fields[extraction.id] = [
                (path, value) for path, value in values.items() if path not in emitted
            ]
            sides.append((label, extraction, fields))

        page = TEMPLATES.TemplateResponse(
            request,
            "run_review.html",
            {
                "run": run,
                "policy": policy,
                "client": client,
                "sides": sides,
                "extra_fields": extra_fields,
            },
        )
        session.close()
        return page

    @app.post("/fields/{field_id}/correct", status_code=204)
    def correct_field(field_id: int, corrected_value: str = Form(...)):
        session = db()
        field = session.get(ExtractedField, field_id)
        if field is None:
            session.close()
            raise HTTPException(status_code=404, detail="no such field")
        record_correction(
            session,
            extraction_id=field.extraction_id,
            extracted_field_id=field.id,
            field_path=field.field_path,
            kind="wrong_value",
            extracted_value=field.value,
            corrected_value=corrected_value,
        )
        session.commit()
        session.close()
        return Response(status_code=204)

    @app.post("/fields/{field_id}/reject", status_code=204)
    def reject_field(field_id: int):
        session = db()
        field = session.get(ExtractedField, field_id)
        if field is None:
            session.close()
            raise HTTPException(status_code=404, detail="no such field")
        record_correction(
            session,
            extraction_id=field.extraction_id,
            extracted_field_id=field.id,
            field_path=field.field_path,
            kind="hallucination",
            extracted_value=field.value,
        )
        session.commit()
        session.close()
        return Response(status_code=204)

    @app.post("/extractions/{extraction_id}/fields", status_code=204)
    def add_missing_field(
        extraction_id: int,
        field_path: str = Form(...),
        corrected_value: str = Form(...),
    ):
        session = db()
        record_correction(
            session,
            extraction_id=extraction_id,
            field_path=field_path,
            kind="omission",
            corrected_value=corrected_value,
        )
        session.commit()
        session.close()
        return Response(status_code=204)

    return app
```

Add a `renewal/app.py` entry point so `uvicorn renewal.app:app` works:

```python
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.extract.runner import AnthropicClient
from renewal.web import create_app
from sqlalchemy.orm import sessionmaker

_settings = load_settings()
app = create_app(
    settings=_settings,
    store=BlobStore(_settings.blob_root),
    model_client=AnthropicClient(_settings.anthropic_api_key),
    session_factory=sessionmaker(bind=get_engine()),
)
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_review.py -v`
Expected: 7 passed

- [ ] **Step 6: Look at it — build-order step 3 is complete**

Run: `uvicorn renewal.app:app --reload`, open `http://127.0.0.1:8000/runs/new`, add a
client and a policy, upload two dec pages, and correct a field. Confirm with
`psql renewal -c "select kind, field_path, extracted_value, corrected_value from correction"`
that the correction was written and the extracted field is unchanged.

- [x] **Step 7: Commit**

```bash
git add renewal/web.py renewal/app.py renewal/templates renewal/static \
        tests/test_web_review.py
git commit -m "feat: review UI with one-keystroke corrections"
```

---

## Task 10: Eval harness

**Files:**
- Create: `evals/__init__.py`, `evals/accuracy.py`, `evals/fixtures/README.md`, `evals/fixtures/progressive-auto-001.json`, `evals/test_extraction.py`, `evals/baseline.json`, `scripts/reextract.py`, `scripts/compare_versions.py`
- Test: `tests/test_accuracy.py`

**Interfaces:**
- Consumes: `extract`, `effective_values`, `ingest_pdf`, `BlobStore`.
- Produces: `Fixture(fixture_id, carrier, pdf_filename, fields: dict[str, str])`; `load_fixtures(directory: Path) -> list[Fixture]`; `score(expected: dict, actual: dict) -> dict[str, bool]`; `accuracy(results: dict[str, bool]) -> float`; `regressions(baseline: dict, current: dict) -> list[str]`; `report(results_by_fixture) -> str`.

- [x] **Step 1: Write the failing tests for the scoring library**

Scoring is pure and gets a normal unit test, so the harness itself is covered by
the default run. Only the extractor test hits the API.

`tests/test_accuracy.py`:

```python
import json

from evals.accuracy import (
    Fixture,
    accuracy,
    load_fixtures,
    regressions,
    report,
    score,
)


def test_score_marks_exact_matches():
    expected = {"policy.total_premium": "1840.00", "coverage.BI.limit_value": "100/300"}
    actual = {"policy.total_premium": "1840.00", "coverage.BI.limit_value": "50/100"}
    assert score(expected, actual) == {
        "policy.total_premium": True,
        "coverage.BI.limit_value": False,
    }


def test_missing_field_scores_false_rather_than_vanishing():
    assert score({"policy.total_premium": "1840.00"}, {}) == {
        "policy.total_premium": False
    }


def test_extra_field_the_fixture_does_not_label_is_ignored():
    result = score({"policy.total_premium": "1"}, {"policy.total_premium": "1", "x": "y"})
    assert result == {"policy.total_premium": True}


def test_accuracy_is_the_fraction_correct():
    assert accuracy({"a": True, "b": False, "c": True}) == 2 / 3


def test_regressions_names_fields_that_passed_before_and_fail_now():
    baseline = {"f1": {"a": True, "b": False}}
    current = {"f1": {"a": False, "b": False}}
    assert regressions(baseline, current) == ["f1:a"]


def test_newly_passing_fields_are_not_regressions():
    assert regressions({"f1": {"a": False}}, {"f1": {"a": True}}) == []


def test_load_fixtures_reads_labelled_json(tmp_path):
    (tmp_path / "one.json").write_text(
        json.dumps(
            {
                "fixture_id": "progressive-auto-001",
                "carrier": "Progressive",
                "pdf_filename": "progressive-auto-001.pdf",
                "fields": {"policy.total_premium": "1840.00"},
            }
        )
    )
    fixtures = load_fixtures(tmp_path)
    assert fixtures == [
        Fixture(
            fixture_id="progressive-auto-001",
            carrier="Progressive",
            pdf_filename="progressive-auto-001.pdf",
            fields={"policy.total_premium": "1840.00"},
        )
    ]


def test_report_breaks_accuracy_down_by_carrier_and_field_path():
    text = report(
        {
            "f1": {"carrier": "Progressive", "fields": {"policy.total_premium": True}},
            "f2": {"carrier": "Progressive", "fields": {"policy.total_premium": False}},
        }
    )
    assert "Progressive" in text
    assert "policy.total_premium" in text
    assert "50" in text  # 1 of 2 correct
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_accuracy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evals.accuracy'`

- [x] **Step 3: Write the scoring library**

`evals/__init__.py`: empty file.

`evals/accuracy.py`:

```python
"""Scoring for the eval harness. Pure functions, no API calls, so the harness
itself is tested in the default run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    carrier: str
    pdf_filename: str
    fields: dict[str, str]


def load_fixtures(directory: Path) -> list[Fixture]:
    fixtures = []
    for path in sorted(Path(directory).glob("*.json")):
        data = json.loads(path.read_text())
        fixtures.append(
            Fixture(
                fixture_id=data["fixture_id"],
                carrier=data["carrier"],
                pdf_filename=data["pdf_filename"],
                fields=data["fields"],
            )
        )
    return fixtures


def score(expected: dict[str, str], actual: dict[str, str | None]) -> dict[str, bool]:
    """One boolean per labelled field. Fields the fixture does not label are
    ignored: the fixture is the ground truth about what should be found."""
    return {path: actual.get(path) == value for path, value in expected.items()}


def accuracy(results: dict[str, bool]) -> float:
    if not results:
        return 0.0
    return sum(results.values()) / len(results)


def regressions(
    baseline: dict[str, dict[str, bool]], current: dict[str, dict[str, bool]]
) -> list[str]:
    """Fields that passed in the baseline and fail now. These fail the build."""
    out = []
    for fixture_id, fields in baseline.items():
        for path, passed in fields.items():
            if passed and not current.get(fixture_id, {}).get(path, False):
                out.append(f"{fixture_id}:{path}")
    return sorted(out)


def report(results_by_fixture: dict[str, dict]) -> str:
    """Per-carrier and per-field-path accuracy, as plain text."""
    by_carrier: dict[str, list[bool]] = {}
    by_path: dict[str, list[bool]] = {}
    for entry in results_by_fixture.values():
        for path, passed in entry["fields"].items():
            by_carrier.setdefault(entry["carrier"], []).append(passed)
            by_path.setdefault(path, []).append(passed)

    lines = ["", "accuracy by carrier:"]
    for carrier, values in sorted(by_carrier.items()):
        pct = 100 * sum(values) / len(values)
        lines.append(f"  {carrier:<24} {pct:5.1f}%  ({sum(values)}/{len(values)})")
    lines.append("")
    lines.append("accuracy by field path:")
    for path, values in sorted(by_path.items()):
        pct = 100 * sum(values) / len(values)
        lines.append(f"  {path:<44} {pct:5.1f}%  ({sum(values)}/{len(values)})")
    return "\n".join(lines)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_accuracy.py -v`
Expected: 8 passed

- [x] **Step 5: Write the fixture format and the API-hitting harness**

`evals/fixtures/README.md`:

```markdown
# Fixtures

One JSON file per labelled document. The PDF itself lives in `evals/pdfs/`,
which is gitignored — real client documents never enter the repo.

Redact before labelling: replace names, street addresses, and full VINs with
stable stand-ins, and keep the redaction consistent between the JSON and any
notes. Values that carry no identity (premiums, limits, deductibles, dates,
coverage codes) are labelled as they appear, because those are what the
extractor is scored on.

Aim for 10 to start, 20 before trusting a version comparison.
```

`evals/fixtures/progressive-auto-001.json` — the shape to copy, using redacted values:

```json
{
  "fixture_id": "progressive-auto-001",
  "carrier": "Progressive",
  "pdf_filename": "progressive-auto-001.pdf",
  "fields": {
    "policy.carrier_name": "Progressive",
    "policy.policy_number": "AU-0000001",
    "policy.effective_date": "2026-03-01",
    "policy.expiration_date": "2026-09-01",
    "policy.total_premium": "1840.00",
    "coverage.BI.limit_value": "100/300",
    "coverage.PD.limit_value": "50000",
    "item.REDACTEDVIN0000001.descriptor": "2018 Ford F-150",
    "item.REDACTEDVIN0000001.coverage.COLL.deductible_value": "500",
    "item.REDACTEDVIN0000001.coverage.COLL.premium": "412.00"
  }
}
```

`evals/test_extraction.py`:

```python
"""Runs the current extractor against every labelled fixture.

Marked `eval` because it makes real API calls; excluded from the default run.
Run it with: pytest -m eval evals/test_extraction.py -s
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from evals.accuracy import accuracy, load_fixtures, regressions, report, score
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.corrections import effective_values
from renewal.extract.runner import AnthropicClient, extract
from renewal.ingest import ingest_pdf

FIXTURE_DIR = Path(__file__).parent / "fixtures"
PDF_DIR = Path(__file__).parent / "pdfs"
BASELINE = Path(__file__).parent / "baseline.json"
VERSION = "v1"


@pytest.mark.eval
def test_extractor_accuracy_against_fixtures(engine, tmp_path, capsys):
    fixtures = load_fixtures(FIXTURE_DIR)
    available = [f for f in fixtures if (PDF_DIR / f.pdf_filename).exists()]
    if not available:
        pytest.skip("no fixture PDFs present in evals/pdfs/")

    settings = load_settings()
    store = BlobStore(tmp_path / "blobs")
    client = AnthropicClient(settings.anthropic_api_key)
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
        }
    session.rollback()
    session.close()

    with capsys.disabled():
        print(report(results))
        for fixture_id, entry in sorted(results.items()):
            print(f"  {fixture_id:<28} {100 * accuracy(entry['fields']):5.1f}%")

    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text())
        broken = regressions(
            {k: v["fields"] for k, v in baseline.items()},
            {k: v["fields"] for k, v in results.items()},
        )
        assert not broken, f"fields that used to pass and now fail: {broken}"

    BASELINE.with_suffix(".latest.json").write_text(json.dumps(results, indent=2))
```

`evals/baseline.json` — start it as an empty object; promote a run by copying
`evals/baseline.latest.json` over it once its numbers are accepted:

```json
{}
```

- [x] **Step 6: Write the corpus scripts**

`scripts/reextract.py`:

```python
"""Re-run an extractor version over every document in the corpus.

Usage: python scripts/reextract.py v2

Writes new extraction rows. Nothing existing is touched, so old and new results
can be compared afterwards.
"""

from __future__ import annotations

import sys

from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.db import session_scope
from renewal.extract.runner import AnthropicClient, extract
from renewal.models import Document


def main(version: str) -> None:
    settings = load_settings()
    store = BlobStore(settings.blob_root)
    client = AnthropicClient(settings.anthropic_api_key)
    with session_scope() as session:
        documents = session.query(Document).order_by(Document.id).all()
        for document in documents:
            extraction = extract(
                session, store, document, version, client=client, settings=settings
            )
            print(f"document {document.id} -> extraction {extraction.id} "
                  f"({extraction.status})")


if __name__ == "__main__":
    main(sys.argv[1])
```

`scripts/compare_versions.py`:

```python
"""Per-field accuracy diff between two extractor versions over the fixtures.

Usage: python scripts/compare_versions.py v1 v2

Makes real API calls. Prints one line per field path whose accuracy moved, so a
change that helps overall but quietly breaks one carrier is still visible.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from evals.accuracy import load_fixtures, score
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.corrections import effective_values
from renewal.db import get_engine
from renewal.extract.runner import AnthropicClient, extract
from renewal.ingest import ingest_pdf

FIXTURE_DIR = Path(__file__).parent.parent / "evals" / "fixtures"
PDF_DIR = Path(__file__).parent.parent / "evals" / "pdfs"


def run_version(version: str) -> dict[str, dict[str, bool]]:
    settings = load_settings()
    client = AnthropicClient(settings.anthropic_api_key)
    session = sessionmaker(bind=get_engine())()
    out: dict[str, dict[str, bool]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        store = BlobStore(Path(tmp))
        for fixture in load_fixtures(FIXTURE_DIR):
            pdf = PDF_DIR / fixture.pdf_filename
            if not pdf.exists():
                continue
            document = ingest_pdf(
                session, store, data=pdf.read_bytes(),
                original_filename=fixture.pdf_filename,
            )
            extraction = extract(
                session, store, document, version, client=client, settings=settings
            )
            out[fixture.fixture_id] = score(
                fixture.fields, effective_values(session, extraction.id)
            )
    session.rollback()
    session.close()
    return out


def main(version_a: str, version_b: str) -> None:
    a, b = run_version(version_a), run_version(version_b)
    paths = {p for fields in a.values() for p in fields}
    print(f"{'field path':<44} {version_a:>8} {version_b:>8}   delta")
    for path in sorted(paths):
        a_vals = [f[path] for f in a.values() if path in f]
        b_vals = [f[path] for f in b.values() if path in f]
        a_pct = 100 * sum(a_vals) / len(a_vals) if a_vals else 0.0
        b_pct = 100 * sum(b_vals) / len(b_vals) if b_vals else 0.0
        if a_pct != b_pct:
            print(f"{path:<44} {a_pct:7.1f}% {b_pct:7.1f}%  {b_pct - a_pct:+6.1f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

- [ ] **Step 7: Label ten fixtures and record the first baseline**

Put ten real dec pages in `evals/pdfs/` (gitignored), write one redacted JSON
per document in `evals/fixtures/`, then run:

```bash
pytest -m eval evals/test_extraction.py -s
cp evals/baseline.latest.json evals/baseline.json
```

Read the per-field report before accepting the baseline. A field that is wrong
everywhere is a prompt problem; a field wrong on one carrier is a document
problem.

- [x] **Step 8: Commit — build-order step 4 is complete**

```bash
git add evals scripts/reextract.py scripts/compare_versions.py tests/test_accuracy.py
git commit -m "feat: eval harness with per-field accuracy and regression gate"
```

---
## Task 11: Promotion

**Files:**
- Create: `renewal/promote.py`
- Test: `tests/test_promote.py`

**Interfaces:**
- Consumes: `effective_values` (Task 8), `Extraction`, `ExtractedField`, `PolicyTerm`, `Coverage`, `InsuredItem` (Task 2).
- Produces: `PromotionBlocked(paths: list[str])`; `unresolved_field_paths(session, extraction_id, acknowledged=frozenset()) -> list[str]`; `promote(session, extraction, policy_id, *, acknowledged=frozenset()) -> PolicyTerm`.

Forms are stored as `insured_item` rows with `item_type="form"`, descriptor set to
the form number and `attributes={"edition_date": ...}`. They are not insured
things, but they must live on the term for the diff to see them, and the
`forms.<number>.edition_date` noise rule depends on that.

- [x] **Step 1: Write the failing tests**

`tests/test_promote.py`:

```python
import datetime as dt

import pytest

from renewal.corrections import record_correction
from renewal.models import (
    Client,
    Coverage,
    Document,
    ExtractedField,
    Extraction,
    InsuredItem,
    Policy,
)
from renewal.promote import PromotionBlocked, promote, unresolved_field_paths

FIELDS = {
    "policy.carrier_name": ("Progressive", 0.99),
    "policy.policy_number": ("AU-4471", 0.99),
    "policy.effective_date": ("2026-03-01", 0.98),
    "policy.expiration_date": ("2026-09-01", 0.98),
    "policy.total_premium": ("1840.00", 0.97),
    "coverage.BI.limit_value": ("100/300", 0.95),
    "item.VIN0001.descriptor": ("2018 Ford F-150", 0.95),
    "item.VIN0001.attributes.garaging_zip": ("78704", 0.92),
    "item.VIN0001.coverage.COLL.deductible_value": ("500", 0.94),
    "item.VIN0001.coverage.COLL.premium": ("412.00", 0.93),
    "forms.A085.edition_date": ("2019-06", 0.90),
}


@pytest.fixture
def policy(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    return policy


@pytest.fixture
def extraction(session):
    document = Document(
        blob_sha256="e" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    extraction = Extraction(
        document_id=document.id,
        extractor_version="v1",
        model_id="claude-opus-5",
        status="ok",
    )
    session.add(extraction)
    session.flush()
    for path, (value, confidence) in FIELDS.items():
        session.add(
            ExtractedField(
                extraction_id=extraction.id,
                field_path=path,
                value=value,
                confidence=confidence,
                source_page=1,
                source_text_span=value,
                needs_review=confidence < 0.80,
            )
        )
    session.flush()
    return extraction


def test_promotion_writes_a_term_with_policy_scalars(session, policy, extraction):
    term = promote(session, extraction, policy.id)
    assert term.policy_id == policy.id
    assert term.carrier_name == "Progressive"
    assert term.policy_number == "AU-4471"
    assert term.effective_date == dt.date(2026, 3, 1)
    assert term.expiration_date == dt.date(2026, 9, 1)
    assert term.total_premium == "1840.00"
    assert term.promoted_from_extraction_id == extraction.id
    assert term.source_document_id == extraction.document_id


def test_policy_level_and_vehicle_level_coverages_land_correctly(
    session, policy, extraction
):
    term = promote(session, extraction, policy.id)
    coverages = {c.coverage_code: c for c in term.coverages}
    assert coverages["BI"].insured_item_id is None
    assert coverages["BI"].limit_value == "100/300"

    vehicle = next(i for i in term.items if i.item_type == "vehicle")
    assert vehicle.descriptor == "2018 Ford F-150"
    assert vehicle.attributes["item_key"] == "VIN0001"
    assert vehicle.attributes["garaging_zip"] == "78704"
    assert coverages["COLL"].insured_item_id == vehicle.id
    assert coverages["COLL"].deductible_value == "500"
    assert coverages["COLL"].premium == "412.00"


def test_forms_are_promoted_so_the_diff_can_see_them(session, policy, extraction):
    term = promote(session, extraction, policy.id)
    form = next(i for i in term.items if i.item_type == "form")
    assert form.descriptor == "A085"
    assert form.attributes["edition_date"] == "2019-06"


def test_corrections_are_applied_at_promotion(session, policy, extraction):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="policy.total_premium")
        .one()
    )
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value=field.value,
        corrected_value="1804.00",
    )
    term = promote(session, extraction, policy.id)
    assert term.total_premium == "1804.00"


def test_promotion_is_blocked_by_unresolved_low_confidence_fields(
    session, policy, extraction
):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="coverage.BI.limit_value")
        .one()
    )
    field.needs_review = True
    session.flush()
    with pytest.raises(PromotionBlocked) as excinfo:
        promote(session, extraction, policy.id)
    assert "coverage.BI.limit_value" in excinfo.value.paths


def test_a_correction_resolves_the_block(session, policy, extraction):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="coverage.BI.limit_value")
        .one()
    )
    field.needs_review = True
    session.flush()
    record_correction(
        session,
        extraction_id=extraction.id,
        extracted_field_id=field.id,
        field_path=field.field_path,
        kind="wrong_value",
        extracted_value=field.value,
        corrected_value="250/500",
    )
    assert unresolved_field_paths(session, extraction.id) == []
    assert promote(session, extraction, policy.id) is not None


def test_acknowledging_a_field_also_resolves_the_block(session, policy, extraction):
    field = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction.id, field_path="coverage.BI.limit_value")
        .one()
    )
    field.needs_review = True
    session.flush()
    assert unresolved_field_paths(
        session, extraction.id, acknowledged=frozenset({"coverage.BI.limit_value"})
    ) == []


def test_promoting_twice_creates_a_second_term_and_keeps_the_first(
    session, policy, extraction
):
    first = promote(session, extraction, policy.id)
    second = promote(session, extraction, policy.id)
    assert first.id != second.id
    assert session.get(type(first), first.id).total_premium == "1840.00"
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_promote.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.promote'`

- [x] **Step 3: Write the implementation**

`renewal/promote.py`:

```python
"""Promotion freezes one extraction plus its corrections into a policy_term.

The snapshot is written once and never updated. A later correction, or a better
extractor, produces a new term — so every comparison keeps pointing at exactly
the values it was computed from.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from renewal.corrections import effective_values
from renewal.models import Coverage, ExtractedField, Extraction, InsuredItem, PolicyTerm


class PromotionBlocked(Exception):
    """Raised when fields still need review. Promotion is the human gate."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f"unresolved fields: {', '.join(paths)}")


def unresolved_field_paths(
    session: Session, extraction_id: int, acknowledged: frozenset[str] = frozenset()
) -> list[str]:
    values = effective_values(session, extraction_id)
    flagged = (
        session.query(ExtractedField)
        .filter_by(extraction_id=extraction_id, needs_review=True)
        .order_by(ExtractedField.field_path)
        .all()
    )
    out = []
    for field in flagged:
        if field.field_path in acknowledged:
            continue
        if values.get(field.field_path) != field.value:
            continue  # a correction replaced or removed it
        out.append(field.field_path)
    return out


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def promote(
    session: Session,
    extraction: Extraction,
    policy_id: int,
    *,
    acknowledged: frozenset[str] = frozenset(),
) -> PolicyTerm:
    blocked = unresolved_field_paths(session, extraction.id, acknowledged)
    if blocked:
        raise PromotionBlocked(blocked)

    values = effective_values(session, extraction.id)
    term = PolicyTerm(
        policy_id=policy_id,
        carrier_name=values.get("policy.carrier_name"),
        policy_number=values.get("policy.policy_number"),
        effective_date=_parse_date(values.get("policy.effective_date")),
        expiration_date=_parse_date(values.get("policy.expiration_date")),
        total_premium=values.get("policy.total_premium"),
        source_document_id=extraction.document_id,
        promoted_from_extraction_id=extraction.id,
    )
    session.add(term)
    session.flush()

    # Group the flat field map back into the term's shape.
    policy_coverages: dict[str, dict[str, str | None]] = {}
    items: dict[str, dict] = {}
    forms: dict[str, str | None] = {}

    for path, value in values.items():
        parts = path.split(".")
        if parts[0] == "coverage":
            policy_coverages.setdefault(parts[1], {})[parts[2]] = value
        elif parts[0] == "item":
            item = items.setdefault(
                parts[1], {"descriptor": None, "attributes": {}, "coverages": {}}
            )
            if parts[2] == "descriptor":
                item["descriptor"] = value
            elif parts[2] == "attributes":
                item["attributes"][parts[3]] = value
            elif parts[2] == "coverage":
                item["coverages"].setdefault(parts[3], {})[parts[4]] = value
        elif parts[0] == "forms":
            forms[parts[1]] = value

    for code, leaves in sorted(policy_coverages.items()):
        session.add(
            Coverage(
                policy_term_id=term.id,
                insured_item_id=None,
                coverage_code=code,
                limit_value=leaves.get("limit_value"),
                limit_basis=leaves.get("limit_basis"),
                deductible_value=leaves.get("deductible_value"),
                premium=leaves.get("premium"),
            )
        )

    for key, data in sorted(items.items()):
        attributes = dict(data["attributes"])
        attributes["item_key"] = key
        item_row = InsuredItem(
            policy_term_id=term.id,
            item_type="vehicle",
            descriptor=data["descriptor"],
            attributes=attributes,
        )
        session.add(item_row)
        session.flush()
        for code, leaves in sorted(data["coverages"].items()):
            session.add(
                Coverage(
                    policy_term_id=term.id,
                    insured_item_id=item_row.id,
                    coverage_code=code,
                    limit_value=leaves.get("limit_value"),
                    limit_basis=leaves.get("limit_basis"),
                    deductible_value=leaves.get("deductible_value"),
                    premium=leaves.get("premium"),
                )
            )

    for form_number, edition_date in sorted(forms.items()):
        session.add(
            InsuredItem(
                policy_term_id=term.id,
                item_type="form",
                descriptor=form_number,
                attributes={"item_key": form_number, "edition_date": edition_date},
            )
        )

    session.flush()
    session.refresh(term)
    return term
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_promote.py -v`
Expected: 8 passed

- [x] **Step 5: Commit**

```bash
git add renewal/promote.py tests/test_promote.py
git commit -m "feat: promotion snapshot gated on unresolved fields"
```

---

## Task 12: Diff engine

**Files:**
- Create: `renewal/diff.py`
- Test: `tests/test_diff.py`

**Interfaces:**
- Consumes: `PolicyTerm`, `Coverage`, `InsuredItem` (Task 2).
- Produces: `RawDifference(field_path: str, prior_value: str | None, renewal_value: str | None)`; `term_field_map(session, term) -> dict[str, str | None]`; `normalize(field_path: str, value: str | None) -> str | None`; `diff_terms(session, prior, renewal) -> list[RawDifference]`.

- [x] **Step 1: Write the failing tests**

`tests/test_diff.py`:

```python
import pytest

from renewal.diff import RawDifference, diff_terms, normalize, term_field_map
from renewal.models import Client, Coverage, InsuredItem, Policy, PolicyTerm


@pytest.fixture
def policy(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    return policy


def _term(session, policy, *, premium, vehicles, policy_coverages=None, forms=None):
    term = PolicyTerm(
        policy_id=policy.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        effective_date="2026-03-01",
        total_premium=premium,
    )
    session.add(term)
    session.flush()
    for code, limit in (policy_coverages or {}).items():
        session.add(
            Coverage(policy_term_id=term.id, coverage_code=code, limit_value=limit)
        )
    for key, spec in vehicles.items():
        item = InsuredItem(
            policy_term_id=term.id,
            item_type="vehicle",
            descriptor=spec["descriptor"],
            attributes={"item_key": key},
        )
        session.add(item)
        session.flush()
        for code, leaves in spec.get("coverages", {}).items():
            session.add(
                Coverage(
                    policy_term_id=term.id,
                    insured_item_id=item.id,
                    coverage_code=code,
                    deductible_value=leaves.get("deductible_value"),
                    premium=leaves.get("premium"),
                )
            )
    for number, edition in (forms or {}).items():
        session.add(
            InsuredItem(
                policy_term_id=term.id,
                item_type="form",
                descriptor=number,
                attributes={"item_key": number, "edition_date": edition},
            )
        )
    session.flush()
    return term


def test_term_field_map_uses_the_canonical_grammar(session, policy):
    term = _term(
        session,
        policy,
        premium="1840.00",
        policy_coverages={"BI": "100/300"},
        vehicles={
            "VIN0001": {
                "descriptor": "2018 Ford F-150",
                "coverages": {"COLL": {"deductible_value": "500", "premium": "412.00"}},
            }
        },
        forms={"A085": "2019-06"},
    )
    field_map = term_field_map(session, term)
    assert field_map["policy.total_premium"] == "1840.00"
    assert field_map["coverage.BI.limit_value"] == "100/300"
    assert field_map["item.VIN0001.coverage.COLL.deductible_value"] == "500"
    assert field_map["forms.A085.edition_date"] == "2019-06"


def test_money_is_normalized_before_comparison():
    assert normalize("policy.total_premium", "$1,840.00") == normalize(
        "policy.total_premium", "1840.0"
    )


def test_whitespace_is_normalized_before_comparison():
    assert normalize("item.VIN1.descriptor", "2018  Ford   F-150") == normalize(
        "item.VIN1.descriptor", "2018 Ford F-150"
    )


def test_unchanged_fields_produce_no_difference(session, policy):
    kwargs = dict(premium="1840.00", vehicles={"VIN0001": {"descriptor": "F-150"}})
    prior = _term(session, policy, **kwargs)
    renewal = _term(session, policy, **kwargs)
    assert diff_terms(session, prior, renewal) == []


def test_changed_premium_produces_a_difference_with_raw_values(session, policy):
    prior = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    renewal = _term(
        session, policy, premium="2180.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    assert RawDifference(
        field_path="policy.total_premium",
        prior_value="1840.00",
        renewal_value="2180.00",
    ) in diff_terms(session, prior, renewal)


def test_added_vehicle_appears_as_a_one_sided_difference(session, policy):
    prior = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    renewal = _term(
        session,
        policy,
        premium="2180.00",
        vehicles={"V1": {"descriptor": "F-150"}, "V2": {"descriptor": "2019 Civic"}},
    )
    added = [
        d for d in diff_terms(session, prior, renewal)
        if d.field_path == "item.V2.descriptor"
    ]
    assert added == [
        RawDifference(
            field_path="item.V2.descriptor",
            prior_value=None,
            renewal_value="2019 Civic",
        )
    ]


def test_dropped_vehicle_appears_as_a_one_sided_difference(session, policy):
    prior = _term(
        session,
        policy,
        premium="1840.00",
        vehicles={"V1": {"descriptor": "F-150"}, "V2": {"descriptor": "2019 Civic"}},
    )
    renewal = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    dropped = [
        d for d in diff_terms(session, prior, renewal)
        if d.field_path == "item.V2.descriptor"
    ]
    assert dropped[0].renewal_value is None


def test_per_vehicle_deductibles_do_not_collide(session, policy):
    prior = _term(
        session,
        policy,
        premium="1840.00",
        vehicles={
            "V1": {"descriptor": "F-150", "coverages": {"COLL": {"deductible_value": "500"}}},
            "V2": {"descriptor": "Civic", "coverages": {"COLL": {"deductible_value": "500"}}},
        },
    )
    renewal = _term(
        session,
        policy,
        premium="1840.00",
        vehicles={
            "V1": {"descriptor": "F-150", "coverages": {"COLL": {"deductible_value": "1000"}}},
            "V2": {"descriptor": "Civic", "coverages": {"COLL": {"deductible_value": "500"}}},
        },
    )
    paths = [d.field_path for d in diff_terms(session, prior, renewal)]
    assert paths == ["item.V1.coverage.COLL.deductible_value"]


def test_sub_dollar_rounding_still_emits_a_difference(session, policy):
    """Noise is a label, not a filter — the row must exist to be classified."""
    prior = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    renewal = _term(
        session, policy, premium="1840.40", vehicles={"V1": {"descriptor": "F-150"}}
    )
    assert any(
        d.field_path == "policy.total_premium"
        for d in diff_terms(session, prior, renewal)
    )
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_diff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.diff'`

- [x] **Step 3: Write the implementation**

`renewal/diff.py`:

```python
"""Diffing two promoted terms.

Every difference is emitted. Nothing is suppressed here — classification labels
rows later, so the audit trail stays complete and it stays visible how often
each noise rule fires. Matching falls out of the field-path keys: two values
compare only when they describe the same thing on the same vehicle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from renewal.models import Coverage, InsuredItem, PolicyTerm

MONEY_LEAVES = ("total_premium", "premium", "deductible_value")


@dataclass(frozen=True)
class RawDifference:
    field_path: str
    prior_value: str | None
    renewal_value: str | None


def normalize(field_path: str, value: str | None) -> str | None:
    """Canonicalize type only. Never suppresses a difference."""
    if value is None:
        return None
    text = " ".join(value.split())
    if field_path.split(".")[-1] in MONEY_LEAVES:
        try:
            return str(Decimal(re.sub(r"[$,\s]", "", text)).normalize())
        except InvalidOperation:
            return text
    return text


def term_field_map(session: Session, term: PolicyTerm) -> dict[str, str | None]:
    """The term, flattened into canonical field paths."""
    field_map: dict[str, str | None] = {
        "policy.carrier_name": term.carrier_name,
        "policy.policy_number": term.policy_number,
        "policy.effective_date": (
            term.effective_date.isoformat() if term.effective_date else None
        ),
        "policy.expiration_date": (
            term.expiration_date.isoformat() if term.expiration_date else None
        ),
        "policy.total_premium": term.total_premium,
    }

    items = {
        item.id: item
        for item in session.query(InsuredItem).filter_by(policy_term_id=term.id)
    }
    for item in items.values():
        key = item.attributes.get("item_key", item.descriptor)
        if item.item_type == "form":
            field_map[f"forms.{key}.edition_date"] = item.attributes.get("edition_date")
            continue
        field_map[f"item.{key}.descriptor"] = item.descriptor
        for name, value in item.attributes.items():
            if name == "item_key":
                continue
            field_map[f"item.{key}.attributes.{name}"] = value

    for coverage in session.query(Coverage).filter_by(policy_term_id=term.id):
        if coverage.insured_item_id is None:
            prefix = f"coverage.{coverage.coverage_code}"
        else:
            item = items[coverage.insured_item_id]
            key = item.attributes.get("item_key", item.descriptor)
            prefix = f"item.{key}.coverage.{coverage.coverage_code}"
        for leaf in ("limit_value", "limit_basis", "deductible_value", "premium"):
            value = getattr(coverage, leaf)
            if value is not None:
                field_map[f"{prefix}.{leaf}"] = value

    return {path: value for path, value in field_map.items() if value is not None}


def diff_terms(
    session: Session, prior: PolicyTerm, renewal: PolicyTerm
) -> list[RawDifference]:
    prior_map = term_field_map(session, prior)
    renewal_map = term_field_map(session, renewal)
    differences = []
    for path in sorted(set(prior_map) | set(renewal_map)):
        before, after = prior_map.get(path), renewal_map.get(path)
        if normalize(path, before) != normalize(path, after):
            differences.append(RawDifference(path, before, after))
    return differences
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_diff.py -v`
Expected: 9 passed

- [x] **Step 5: Commit**

```bash
git add renewal/diff.py tests/test_diff.py
git commit -m "feat: term diff with per-vehicle coverage matching"
```

---

## Task 13: Materiality rules

**Files:**
- Create: `renewal/materiality.py`, `config/materiality.yaml`
- Test: `tests/test_materiality.py`

**Interfaces:**
- Consumes: `RawDifference` (Task 12), `fieldpath.glob_match` (Task 5).
- Produces: `Rule`, `RuleSet`, `load_rules(path: Path) -> RuleSet`, `classify(difference, rules) -> tuple[str, str]` returning `(materiality, rule_id)`.

- [x] **Step 1: Write the failing tests**

`tests/test_materiality.py`:

```python
from pathlib import Path

import pytest

from renewal.diff import RawDifference
from renewal.materiality import classify, load_rules

RULES = Path("config/materiality.yaml")


@pytest.fixture(scope="module")
def rules():
    return load_rules(RULES)


def test_large_premium_change_is_material(rules):
    assert classify(
        RawDifference("policy.total_premium", "1840.00", "2180.00"), rules
    ) == ("material", "premium_total_change")


def test_premium_change_under_both_thresholds_is_noise(rules):
    materiality, rule_id = classify(
        RawDifference("policy.total_premium", "1840.00", "1840.40"), rules
    )
    assert materiality == "noise"
    assert rule_id == "premium_rounding"


def test_deductible_change_is_material_at_either_level(rules):
    assert classify(
        RawDifference("item.V1.coverage.COLL.deductible_value", "500", "1000"), rules
    )[0] == "material"
    assert classify(
        RawDifference("coverage.COMP.deductible_value", "500", "1000"), rules
    )[0] == "material"


def test_limit_change_is_material(rules):
    assert classify(
        RawDifference("coverage.BI.limit_value", "100/300", "50/100"), rules
    )[0] == "material"


def test_added_vehicle_is_material(rules):
    assert classify(
        RawDifference("item.V2.descriptor", None, "2019 Honda Civic"), rules
    )[0] == "material"


def test_carrier_change_is_material(rules):
    assert classify(
        RawDifference("policy.carrier_name", "Progressive", "Safeco"), rules
    )[0] == "material"


def test_garaging_address_change_is_informational(rules):
    assert classify(
        RawDifference("item.V1.attributes.garaging_zip", "78704", "78745"), rules
    )[0] == "informational"


def test_form_edition_change_is_noise(rules):
    assert classify(
        RawDifference("forms.A085.edition_date", "2019-06", "2024-01"), rules
    ) == ("noise", "form_edition")


def test_policy_number_reformatting_is_noise(rules):
    assert classify(
        RawDifference("policy.policy_number", "AU-4471", "AU4471"), rules
    ) == ("noise", "policy_number_format")


def test_unmatched_path_falls_to_the_default_rule(rules):
    assert classify(
        RawDifference("item.V1.attributes.odometer", "41000", "58000"), rules
    ) == ("informational", "default")


def test_first_matching_rule_wins(tmp_path):
    (tmp_path / "rules.yaml").write_text(
        "version: 1\n"
        "default: informational\n"
        "rules:\n"
        "  - id: first\n"
        "    match: {path_glob: '**.premium'}\n"
        "    materiality: material\n"
        "  - id: second\n"
        "    match: {path_glob: '**'}\n"
        "    materiality: noise\n"
    )
    rules = load_rules(tmp_path / "rules.yaml")
    assert classify(
        RawDifference("item.V1.coverage.COLL.premium", "412.00", "530.00"), rules
    ) == ("material", "first")
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_materiality.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.materiality'`

- [x] **Step 3: Write the rule set**

`config/materiality.yaml`:

```yaml
# Ordered. First matching rule wins. The rule that matched is recorded on every
# difference row, so a wrong classification can be traced back to the rule that
# caused it.
#
# `*` matches one path segment, `**` matches any number.
version: 1
default: informational

rules:
  # Premium: a small move is rounding, a real move is the whole conversation.
  - id: premium_rounding
    match: {path: policy.total_premium}
    when: {abs_delta_lt: 1}
    materiality: noise

  - id: premium_total_change
    match: {path: policy.total_premium}
    when: {abs_delta_gte: 25, pct_delta_gte: 0.03, combine: or}
    materiality: material

  - id: premium_total_small_change
    match: {path: policy.total_premium}
    materiality: informational

  # Coverage shape. These are what a client is actually buying.
  - id: deductible_change
    match: {path_glob: "**.deductible_value"}
    materiality: material

  - id: limit_change
    match: {path_glob: "**.limit_value"}
    materiality: material

  - id: limit_basis_change
    match: {path_glob: "**.limit_basis"}
    materiality: material

  - id: coverage_premium_change
    match: {path_glob: "**.coverage.*.premium"}
    materiality: informational

  # Insured items added or removed.
  - id: item_added_or_dropped
    match: {path_glob: "item.*.descriptor"}
    materiality: material

  - id: carrier_change
    match: {path: policy.carrier_name}
    materiality: material

  # Dates and identity.
  - id: policy_number_format
    match: {path: policy.policy_number}
    materiality: noise

  - id: term_dates
    match: {path_glob: "policy.*_date"}
    materiality: informational

  # Vehicle attributes: garaging, lienholder, and the like.
  - id: garaging_change
    match: {path_glob: "item.*.attributes.garaging_*"}
    materiality: informational

  - id: lienholder_change
    match: {path_glob: "item.*.attributes.lienholder*"}
    materiality: informational

  # Paperwork.
  - id: form_edition
    match: {path_glob: "forms.*.edition_date"}
    materiality: noise
```

- [x] **Step 4: Write the implementation**

`renewal/materiality.py`:

```python
"""Materiality classification.

The naive diff produces around forty differences per renewal, of which perhaps
three matter to a client. The filtering is the product, so it lives in a config
file that can be tuned without touching the extractor — not buried in a prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from renewal.diff import RawDifference
from renewal.fieldpath import glob_match

MATERIALITIES = ("material", "informational", "noise")


@dataclass(frozen=True)
class Rule:
    id: str
    materiality: str
    path: str | None = None
    path_glob: str | None = None
    when: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuleSet:
    version: int
    default: str
    rules: list[Rule]


def load_rules(path: Path) -> RuleSet:
    data = yaml.safe_load(Path(path).read_text())
    rules = []
    for entry in data["rules"]:
        if entry["materiality"] not in MATERIALITIES:
            raise ValueError(
                f"rule {entry['id']}: unknown materiality {entry['materiality']}"
            )
        match = entry.get("match", {})
        rules.append(
            Rule(
                id=entry["id"],
                materiality=entry["materiality"],
                path=match.get("path"),
                path_glob=match.get("path_glob"),
                when=entry.get("when", {}),
            )
        )
    return RuleSet(version=data["version"], default=data["default"], rules=rules)


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(re.sub(r"[$,\s]", "", value))
    except InvalidOperation:
        return None


def _conditions_hold(rule: Rule, difference: RawDifference) -> bool:
    """Conditions that need numbers are false when the values are not numbers."""
    if not rule.when:
        return True
    before = _to_decimal(difference.prior_value)
    after = _to_decimal(difference.renewal_value)
    checks = []
    if "abs_delta_gte" in rule.when:
        threshold = Decimal(str(rule.when["abs_delta_gte"]))
        checks.append(
            before is not None and after is not None and abs(after - before) >= threshold
        )
    if "abs_delta_lt" in rule.when:
        threshold = Decimal(str(rule.when["abs_delta_lt"]))
        checks.append(
            before is not None and after is not None and abs(after - before) < threshold
        )
    if "pct_delta_gte" in rule.when:
        threshold = Decimal(str(rule.when["pct_delta_gte"]))
        checks.append(
            before not in (None, Decimal(0))
            and after is not None
            and abs((after - before) / before) >= threshold
        )
    if not checks:
        return True
    return any(checks) if rule.when.get("combine") == "or" else all(checks)


def _matches_path(rule: Rule, path: str) -> bool:
    if rule.path is not None:
        return rule.path == path
    if rule.path_glob is not None:
        return glob_match(rule.path_glob, path)
    return False


def classify(difference: RawDifference, rules: RuleSet) -> tuple[str, str]:
    """Returns (materiality, rule_id). First matching rule wins."""
    for rule in rules.rules:
        if _matches_path(rule, difference.field_path) and _conditions_hold(
            rule, difference
        ):
            return rule.materiality, rule.id
    return rules.default, "default"
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_materiality.py -v`
Expected: 11 passed

- [x] **Step 6: Commit**

```bash
git add renewal/materiality.py config/materiality.yaml tests/test_materiality.py
git commit -m "feat: declarative materiality rules with recorded rule ids"
```

---

## Task 14: Premium attribution

**Files:**
- Create: `renewal/premium.py`
- Test: `tests/test_premium.py`

**Interfaces:**
- Consumes: `term_field_map` output shape (Task 12).
- Produces: `Attribution(label: str, field_path: str | None, amount: Decimal)`; `PremiumBreakdown(available: bool, total_delta: Decimal | None, lines: list[Attribution], residual: Decimal | None, reason: str | None)`; `attribute_premium(prior_map, renewal_map) -> PremiumBreakdown`.

- [x] **Step 1: Write the failing tests**

`tests/test_premium.py`:

```python
from decimal import Decimal

from renewal.premium import attribute_premium


def test_no_total_premium_means_no_attribution():
    result = attribute_premium({}, {})
    assert result.available is False
    assert result.reason == "total premium is not present on both documents"
    assert result.lines == []


def test_matched_coverage_delta_is_attributed():
    prior = {
        "policy.total_premium": "1840.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    renewal = {
        "policy.total_premium": "1958.00",
        "item.V1.coverage.COLL.premium": "530.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.total_delta == Decimal("118.00")
    assert result.lines[0].field_path == "item.V1.coverage.COLL.premium"
    assert result.lines[0].amount == Decimal("118.00")
    assert result.residual == Decimal("0.00")


def test_added_item_contributes_its_whole_premium():
    prior = {"policy.total_premium": "1840.00"}
    renewal = {
        "policy.total_premium": "1994.00",
        "item.V2.coverage.COLL.premium": "154.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.lines[0].amount == Decimal("154.00")
    assert result.residual == Decimal("0.00")


def test_dropped_item_contributes_a_negative_amount():
    prior = {
        "policy.total_premium": "1994.00",
        "item.V2.coverage.COLL.premium": "154.00",
    }
    renewal = {"policy.total_premium": "1840.00"}
    result = attribute_premium(prior, renewal)
    assert result.lines[0].amount == Decimal("-154.00")
    assert result.residual == Decimal("0.00")


def test_unexplained_remainder_is_reported_as_residual():
    prior = {
        "policy.total_premium": "1840.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    renewal = {
        "policy.total_premium": "2180.00",
        "item.V1.coverage.COLL.premium": "530.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.total_delta == Decimal("340.00")
    assert sum(line.amount for line in result.lines) == Decimal("118.00")
    assert result.residual == Decimal("222.00")


def test_unchanged_line_premiums_are_not_listed():
    prior = {
        "policy.total_premium": "1840.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    renewal = {
        "policy.total_premium": "1900.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.lines == []
    assert result.residual == Decimal("60.00")


def test_unparseable_premium_is_treated_as_missing():
    result = attribute_premium(
        {"policy.total_premium": "see attached"},
        {"policy.total_premium": "2180.00"},
    )
    assert result.available is False


def test_labels_name_the_vehicle_and_coverage():
    prior = {"policy.total_premium": "1840.00"}
    renewal = {
        "policy.total_premium": "1994.00",
        "item.1FTEW1EP0JKD00001.coverage.COLL.premium": "154.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.lines[0].label == "COLL on 1FTEW1EP0JKD00001"
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_premium.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.premium'`

- [x] **Step 3: Write the implementation**

`renewal/premium.py`:

```python
"""Arithmetic premium attribution.

A dec page shows what changed, not why. Every line here is a subtraction of two
numbers printed on the documents; nothing is inferred. Whatever the line items
do not account for is reported as a residual and described as not attributable,
because claiming a rate increase the page does not state is exactly the error
nobody would catch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class Attribution:
    label: str
    field_path: str | None
    amount: Decimal


@dataclass(frozen=True)
class PremiumBreakdown:
    available: bool
    total_delta: Decimal | None
    lines: list[Attribution]
    residual: Decimal | None
    reason: str | None = None


def _money(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(re.sub(r"[$,\s]", "", value)).quantize(TWO_PLACES)
    except InvalidOperation:
        return None


def _label(path: str) -> str:
    parts = path.split(".")
    if parts[0] == "item" and "coverage" in parts:
        return f"{parts[parts.index('coverage') + 1]} on {parts[1]}"
    if parts[0] == "coverage":
        return f"{parts[1]} (policy level)"
    return path


def attribute_premium(
    prior_map: dict[str, str | None], renewal_map: dict[str, str | None]
) -> PremiumBreakdown:
    prior_total = _money(prior_map.get("policy.total_premium"))
    renewal_total = _money(renewal_map.get("policy.total_premium"))
    if prior_total is None or renewal_total is None:
        return PremiumBreakdown(
            available=False,
            total_delta=None,
            lines=[],
            residual=None,
            reason="total premium is not present on both documents",
        )

    total_delta = (renewal_total - prior_total).quantize(TWO_PLACES)
    line_paths = sorted(
        path
        for path in set(prior_map) | set(renewal_map)
        if path.endswith(".premium") and path != "policy.total_premium"
    )

    lines = []
    for path in line_paths:
        before = _money(prior_map.get(path)) or Decimal("0.00")
        after = _money(renewal_map.get(path)) or Decimal("0.00")
        delta = (after - before).quantize(TWO_PLACES)
        if delta:
            lines.append(Attribution(label=_label(path), field_path=path, amount=delta))

    attributed = sum((line.amount for line in lines), Decimal("0.00"))
    return PremiumBreakdown(
        available=True,
        total_delta=total_delta,
        lines=lines,
        residual=(total_delta - attributed).quantize(TWO_PLACES),
    )
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_premium.py -v`
Expected: 8 passed

- [x] **Step 5: Commit**

```bash
git add renewal/premium.py tests/test_premium.py
git commit -m "feat: arithmetic premium attribution with explicit residual"
```

---
## Task 15: Comparison assembly and draft generation

**Files:**
- Create: `renewal/comparison.py`, `renewal/draft.py`
- Test: `tests/test_comparison.py`, `tests/test_draft.py`

**Interfaces:**
- Consumes: `diff_terms`, `term_field_map` (Task 12), `classify`, `load_rules` (Task 13), `attribute_premium` (Task 14), `Comparison`, `Difference`, `Draft`, `Reclassification` (Task 2).
- Produces: `build_comparison(session, *, run_id, prior_term, renewal_term, rules) -> Comparison`; `breakdown_for(session, comparison) -> PremiumBreakdown`; `reclassify(session, difference, to_materiality, note=None) -> Reclassification`; `build_prompt(differences, breakdown) -> str`; `generate_draft(session, comparison, differences, breakdown, *, client, settings) -> Draft`; `save_edit(session, draft, final_text) -> Draft`; `latest_draft(session, comparison_id) -> Draft | None`.

- [x] **Step 1: Write the failing tests for comparison assembly**

`tests/test_comparison.py`:

```python
from pathlib import Path

import pytest

from renewal.comparison import breakdown_for, build_comparison, reclassify
from renewal.materiality import load_rules
from renewal.models import (
    Client,
    Coverage,
    Difference,
    InsuredItem,
    Policy,
    PolicyTerm,
    Reclassification,
    RenewalRun,
    Document,
)


@pytest.fixture(scope="module")
def rules():
    return load_rules(Path("config/materiality.yaml"))


@pytest.fixture
def run_and_terms(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    documents = []
    for suffix in ("1", "2"):
        document = Document(
            blob_sha256=suffix * 64,
            original_filename=f"{suffix}.pdf",
            page_count=1,
            has_text_layer=True,
            doc_type="dec_page",
        )
        session.add(document)
        documents.append(document)
    session.flush()
    run = RenewalRun(
        policy_id=policy.id,
        prior_document_id=documents[0].id,
        renewal_document_id=documents[1].id,
    )
    session.add(run)
    session.flush()

    def term(premium, deductible):
        row = PolicyTerm(
            policy_id=policy.id,
            carrier_name="Progressive",
            policy_number="AU-4471",
            effective_date="2026-03-01",
            total_premium=premium,
        )
        session.add(row)
        session.flush()
        item = InsuredItem(
            policy_term_id=row.id,
            item_type="vehicle",
            descriptor="2018 Ford F-150",
            attributes={"item_key": "VIN0001"},
        )
        session.add(item)
        session.flush()
        session.add(
            Coverage(
                policy_term_id=row.id,
                insured_item_id=item.id,
                coverage_code="COLL",
                deductible_value=deductible,
                premium="412.00" if premium == "1840.00" else "530.00",
            )
        )
        session.flush()
        return row

    return run, term("1840.00", "500"), term("2180.00", "1000")


def test_comparison_records_every_difference_with_its_rule(session, run_and_terms, rules):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    rows = session.query(Difference).filter_by(comparison_id=comparison.id).all()
    by_path = {row.field_path: row for row in rows}
    assert by_path["policy.total_premium"].materiality == "material"
    assert by_path["policy.total_premium"].rule_id == "premium_total_change"
    assert (
        by_path["item.VIN0001.coverage.COLL.deductible_value"].rule_id
        == "deductible_change"
    )
    assert all(row.rule_id for row in rows)


def test_comparison_is_frozen_to_the_terms_it_was_built_from(
    session, run_and_terms, rules
):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    assert comparison.prior_term_id == prior.id
    assert comparison.renewal_term_id == renewal.id
    assert comparison.renewal_run_id == run.id


def test_second_comparison_under_the_same_run_keeps_the_first(
    session, run_and_terms, rules
):
    run, prior, renewal = run_and_terms
    first = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    second = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    assert first.id != second.id
    assert session.query(Difference).filter_by(comparison_id=first.id).count() > 0


def test_breakdown_for_reads_the_frozen_terms(session, run_and_terms, rules):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    breakdown = breakdown_for(session, comparison)
    assert breakdown.available is True
    assert str(breakdown.total_delta) == "340.00"
    assert str(breakdown.residual) == "222.00"


def test_reclassifying_logs_the_change_and_leaves_the_difference(
    session, run_and_terms, rules
):
    run, prior, renewal = run_and_terms
    comparison = build_comparison(
        session, run_id=run.id, prior_term=prior, renewal_term=renewal, rules=rules
    )
    difference = (
        session.query(Difference)
        .filter_by(comparison_id=comparison.id, field_path="policy.total_premium")
        .one()
    )
    reclassify(session, difference, "informational", note="client already knew")
    log = session.query(Reclassification).one()
    assert log.from_materiality == "material"
    assert log.to_materiality == "informational"
    assert log.rule_id == "premium_total_change"
    session.refresh(difference)
    assert difference.materiality == "material"  # the difference row is frozen
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_comparison.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.comparison'`

- [x] **Step 3: Write the comparison module**

`renewal/comparison.py`:

```python
"""Assembling a comparison from two frozen terms.

The comparison and its differences are written once. Reclassifying in the UI
logs a reclassification row rather than editing the difference — that log is the
evidence for which rules are wrong.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from renewal.diff import diff_terms, term_field_map
from renewal.materiality import RuleSet, classify
from renewal.models import Comparison, Difference, PolicyTerm, Reclassification
from renewal.premium import PremiumBreakdown, attribute_premium


def build_comparison(
    session: Session,
    *,
    run_id: int,
    prior_term: PolicyTerm,
    renewal_term: PolicyTerm,
    rules: RuleSet,
) -> Comparison:
    comparison = Comparison(
        renewal_run_id=run_id,
        prior_term_id=prior_term.id,
        renewal_term_id=renewal_term.id,
    )
    session.add(comparison)
    session.flush()
    for difference in diff_terms(session, prior_term, renewal_term):
        materiality, rule_id = classify(difference, rules)
        session.add(
            Difference(
                comparison_id=comparison.id,
                field_path=difference.field_path,
                prior_value=difference.prior_value,
                renewal_value=difference.renewal_value,
                materiality=materiality,
                rule_id=rule_id,
            )
        )
    session.flush()
    session.refresh(comparison)
    return comparison


def breakdown_for(session: Session, comparison: Comparison) -> PremiumBreakdown:
    prior = session.get(PolicyTerm, comparison.prior_term_id)
    renewal = session.get(PolicyTerm, comparison.renewal_term_id)
    return attribute_premium(
        term_field_map(session, prior), term_field_map(session, renewal)
    )


def reclassify(
    session: Session,
    difference: Difference,
    to_materiality: str,
    note: str | None = None,
) -> Reclassification:
    log = Reclassification(
        difference_id=difference.id,
        from_materiality=difference.materiality,
        to_materiality=to_materiality,
        rule_id=difference.rule_id,
        note=note,
    )
    session.add(log)
    session.flush()
    return log
```

- [x] **Step 4: Write the failing tests for draft generation**

`tests/test_draft.py`:

```python
from decimal import Decimal

import pytest

from renewal.config import Settings
from renewal.draft import build_prompt, generate_draft, latest_draft, save_edit
from renewal.models import (
    Client,
    Comparison,
    Difference,
    Document,
    Draft,
    Policy,
    PolicyTerm,
    RenewalRun,
)
from renewal.premium import Attribution, PremiumBreakdown


class FakeClient:
    def __init__(self, text="Your premium went up by $340."):
        self.text = text
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append({"model": model, "system": system, "content": content})
        return self.text


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path,
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )


@pytest.fixture
def comparison(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    document = Document(
        blob_sha256="f" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    run = RenewalRun(
        policy_id=policy.id,
        prior_document_id=document.id,
        renewal_document_id=document.id,
    )
    session.add(run)
    session.flush()
    terms = []
    for premium in ("1840.00", "2180.00"):
        term = PolicyTerm(
            policy_id=policy.id, effective_date="2026-03-01", total_premium=premium
        )
        session.add(term)
        session.flush()
        terms.append(term)
    row = Comparison(
        renewal_run_id=run.id,
        prior_term_id=terms[0].id,
        renewal_term_id=terms[1].id,
    )
    session.add(row)
    session.flush()
    return row


def _differences():
    return [
        Difference(
            field_path="policy.total_premium",
            prior_value="1840.00",
            renewal_value="2180.00",
            materiality="material",
            rule_id="premium_total_change",
        ),
        Difference(
            field_path="forms.A085.edition_date",
            prior_value="2019-06",
            renewal_value="2024-01",
            materiality="noise",
            rule_id="form_edition",
        ),
    ]


def _breakdown(residual="222.00"):
    return PremiumBreakdown(
        available=True,
        total_delta=Decimal("340.00"),
        lines=[
            Attribution(
                label="COLL on VIN0001",
                field_path="item.VIN0001.coverage.COLL.premium",
                amount=Decimal("118.00"),
            )
        ],
        residual=Decimal(residual),
    )


def test_prompt_excludes_noise():
    prompt = build_prompt(_differences(), _breakdown())
    assert "policy.total_premium" in prompt
    assert "forms.A085" not in prompt


def test_prompt_states_the_residual_as_unexplained():
    prompt = build_prompt(_differences(), _breakdown())
    assert "222.00" in prompt
    assert "not attributable" in prompt


def test_prompt_says_so_when_attribution_is_unavailable():
    unavailable = PremiumBreakdown(
        available=False,
        total_delta=None,
        lines=[],
        residual=None,
        reason="total premium is not present on both documents",
    )
    assert "not present on both documents" in build_prompt(_differences(), unavailable)


def test_prompt_forbids_advice():
    prompt = build_prompt(_differences(), _breakdown())
    assert "Do not recommend" in prompt
    assert "200 words" in prompt


def test_generate_draft_persists_generated_text(session, settings, comparison):
    client = FakeClient()

    draft = generate_draft(
        session,
        comparison,
        _differences(),
        _breakdown(),
        client=client,
        settings=settings,
    )
    assert draft.generated_text == "Your premium went up by $340."
    assert draft.final_text is None
    assert client.calls[0]["model"] == "claude-sonnet-5"


def test_editing_a_draft_inserts_a_new_row(session, settings, comparison):
    original = generate_draft(
        session,
        comparison,
        _differences(),
        _breakdown(),
        client=FakeClient(),
        settings=settings,
    )
    edited = save_edit(session, original, "Here is what changed on your policy.")

    assert edited.id != original.id
    assert edited.generated_text == original.generated_text
    assert edited.final_text == "Here is what changed on your policy."
    assert edited.edited_at is not None
    session.refresh(original)
    assert original.final_text is None
    assert latest_draft(session, comparison.id).id == edited.id
```

- [x] **Step 5: Run the tests to verify they fail**

Run: `pytest tests/test_draft.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.draft'`

- [x] **Step 6: Write the draft module**

`renewal/draft.py`:

```python
"""Draft generation.

The output is a draft for a licensed human to read and edit, never a message to
a client. It explains what changed; it does not advise, because advising is
licensed activity and not the model's job.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.models import Comparison, Difference, Draft
from renewal.premium import PremiumBreakdown

SYSTEM = """You write short, plain-English notes that an insurance agent sends
to a client explaining what changed at renewal.

Rules:
- Explain what changed. Do not recommend anything, do not advise the client what
  to do, and do not comment on whether their coverage is adequate.
- No jargon. Write "the amount you pay before insurance starts covering a claim"
  rather than "deductible" only if the plain phrasing is clearer; otherwise use
  the ordinary word.
- Use only the facts given below. Do not infer causes. If part of the premium
  change is listed as not attributable, say plainly that the documents do not
  break that part down.
- Under 200 words. No greeting, no signature.
"""


def build_prompt(differences: list[Difference], breakdown: PremiumBreakdown) -> str:
    """Material and informational differences only. Noise never reaches the model."""
    lines = ["Changes at renewal:"]
    for difference in differences:
        if difference.materiality == "noise":
            continue
        before = difference.prior_value if difference.prior_value is not None else "(absent)"
        after = (
            difference.renewal_value if difference.renewal_value is not None else "(absent)"
        )
        lines.append(
            f"- [{difference.materiality}] {difference.field_path}: {before} -> {after}"
        )

    lines.append("")
    if not breakdown.available:
        lines.append(
            f"Premium change cannot be broken down: {breakdown.reason}. Say this "
            "plainly rather than speculating."
        )
        return "\n".join(lines)

    lines.append(f"Total premium change: {breakdown.total_delta}")
    for attribution in breakdown.lines:
        lines.append(f"  {attribution.amount} from {attribution.label}")
    lines.append(
        f"  {breakdown.residual} is not attributable from these documents. Say so; "
        "do not guess at a cause such as a rate increase."
    )
    return "\n".join(lines)


def generate_draft(
    session: Session,
    comparison: Comparison,
    differences: list[Difference],
    breakdown: PremiumBreakdown,
    *,
    client,
    settings: Settings,
) -> Draft:
    text = client.complete(
        model=settings.draft_model,
        system=SYSTEM,
        content=[{"type": "text", "text": build_prompt(differences, breakdown)}],
    )
    draft = Draft(comparison_id=comparison.id, generated_text=text)
    session.add(draft)
    session.flush()
    return draft


def save_edit(session: Session, draft: Draft, final_text: str) -> Draft:
    """An edit is a new row carrying the original generated text. Latest wins."""
    edited = Draft(
        comparison_id=draft.comparison_id,
        generated_text=draft.generated_text,
        final_text=final_text,
        edited_at=dt.datetime.now(dt.timezone.utc),
    )
    session.add(edited)
    session.flush()
    return edited


def latest_draft(session: Session, comparison_id: int) -> Draft | None:
    return (
        session.query(Draft)
        .filter_by(comparison_id=comparison_id)
        .order_by(Draft.id.desc())
        .first()
    )
```

- [x] **Step 7: Run both test files to verify they pass**

Run: `pytest tests/test_comparison.py tests/test_draft.py -v`
Expected: 11 passed

- [x] **Step 8: Commit**

```bash
git add renewal/comparison.py renewal/draft.py tests/test_comparison.py \
        tests/test_draft.py
git commit -m "feat: comparison assembly, reclassification log, draft generation"
```

---

## Task 16: Comparison screen

**Files:**
- Create: `renewal/templates/comparison.html`
- Modify: `renewal/web.py` (add promote, comparison, draft-edit, and reclassify routes), `renewal/templates/run_review.html` (add the promote form)
- Test: `tests/test_web_comparison.py`

**Interfaces:**
- Consumes: `promote`, `PromotionBlocked` (Task 11), `build_comparison`, `breakdown_for`, `reclassify` (Task 15), `generate_draft`, `save_edit`, `latest_draft` (Task 15), `load_rules` (Task 13).
- Produces: routes `POST /runs/{run_id}/promote`, `GET /comparisons/{comparison_id}`, `POST /comparisons/{comparison_id}/draft`, `POST /differences/{difference_id}/reclassify`.

- [x] **Step 1: Write the failing tests**

`tests/test_web_comparison.py`:

```python
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.models import (
    Client,
    Comparison,
    Difference,
    Draft,
    Extraction,
    Policy,
    PolicyTerm,
    Reclassification,
    RenewalRun,
)
from renewal.web import create_app
from tests.pdfmaker import make_text_pdf

PRIOR = ["PROGRESSIVE AUTO", "Total Policy Premium $1,840.00"]
RENEWAL = ["PROGRESSIVE AUTO", "Total Policy Premium $2,180.00"]


class ScriptedClient:
    """Returns extraction JSON for the first two calls, draft text after."""

    def __init__(self):
        self.calls = 0

    def complete(self, *, model, system, content):
        self.calls += 1
        if self.calls == 1:
            return self._extraction("1840.00", "Total Policy Premium $1,840.00")
        if self.calls == 2:
            return self._extraction("2180.00", "Total Policy Premium $2,180.00")
        return "Your renewal premium is $340 higher than last term."

    @staticmethod
    def _extraction(premium, source):
        return json.dumps(
            {
                "fields": [
                    {
                        "field_path": "policy.total_premium",
                        "value": premium,
                        "confidence": 0.95,
                        "source_page": 1,
                        "source_text": source,
                    }
                ]
            }
        )


@pytest.fixture
def app(engine, clean_db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config="config/materiality.yaml",
    )
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=ScriptedClient(),
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def policy_id(engine):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Ramirez Landscaping")
    sess.add(client)
    sess.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    sess.add(policy)
    sess.commit()
    out = policy.id
    sess.close()
    yield out


def _run(client, policy_id):
    response = client.post(
        "/runs",
        data={"policy_id": str(policy_id)},
        files={
            "prior": ("prior.pdf", make_text_pdf([PRIOR]), "application/pdf"),
            "renewal": ("renewal.pdf", make_text_pdf([RENEWAL]), "application/pdf"),
        },
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


def test_promote_builds_terms_comparison_and_draft(app, policy_id, engine):
    with TestClient(app) as client:
        run_id = _run(client, policy_id)
        response = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "/comparisons/" in response.headers["location"]

    sess = sessionmaker(bind=engine)()
    assert sess.query(PolicyTerm).count() == 2
    assert sess.query(Comparison).count() == 1
    assert sess.query(Draft).count() == 1
    assert sess.query(Difference).count() >= 1
    sess.close()


def test_comparison_page_shows_draft_beside_the_diff(app, policy_id):
    with TestClient(app) as client:
        run_id = _run(client, policy_id)
        location = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        ).headers["location"]
        page = client.get(location)
    assert "$340 higher" in page.text
    assert "policy.total_premium" in page.text
    assert "1840.00" in page.text
    assert "2180.00" in page.text
    assert "not attributable" in page.text


def test_editing_the_draft_writes_a_new_row(app, policy_id, engine):
    with TestClient(app) as client:
        run_id = _run(client, policy_id)
        location = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        ).headers["location"]
        comparison_id = int(location.split("/")[-1])
        client.post(
            f"/comparisons/{comparison_id}/draft",
            data={"final_text": "Edited by the agent."},
            follow_redirects=False,
        )

    sess = sessionmaker(bind=engine)()
    drafts = sess.query(Draft).order_by(Draft.id).all()
    assert len(drafts) == 2
    assert drafts[0].final_text is None
    assert drafts[1].final_text == "Edited by the agent."
    sess.close()


def test_reclassifying_from_the_ui_is_logged(app, policy_id, engine):
    with TestClient(app) as client:
        run_id = _run(client, policy_id)
        location = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        ).headers["location"]
        sess = sessionmaker(bind=engine)()
        difference_id = sess.query(Difference).first().id
        sess.close()
        response = client.post(
            f"/differences/{difference_id}/reclassify",
            data={"to_materiality": "noise"},
        )
    assert response.status_code == 204

    sess = sessionmaker(bind=engine)()
    assert sess.query(Reclassification).count() == 1
    sess.close()


def test_promote_is_refused_while_a_field_needs_review(app, policy_id, engine):
    with TestClient(app) as client:
        run_id = _run(client, policy_id)
        sess = sessionmaker(bind=engine)()
        for extraction in sess.query(Extraction).all():
            for field in extraction.fields:
                field.needs_review = True
        sess.commit()
        sess.close()

        response = client.post(
            f"/runs/{run_id}/promote", data={}, follow_redirects=False
        )
    assert response.status_code == 400
    assert "policy.total_premium" in response.text


def test_acknowledging_a_field_allows_promotion(app, policy_id, engine):
    with TestClient(app) as client:
        run_id = _run(client, policy_id)
        sess = sessionmaker(bind=engine)()
        for extraction in sess.query(Extraction).all():
            for field in extraction.fields:
                field.needs_review = True
        sess.commit()
        sess.close()

        response = client.post(
            f"/runs/{run_id}/promote",
            data={"acknowledged": "policy.total_premium"},
            follow_redirects=False,
        )
    assert response.status_code == 303
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_web_comparison.py -v`
Expected: FAIL — 404 on `/runs/{id}/promote`

- [x] **Step 3: Write the comparison template**

`renewal/templates/comparison.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Comparison #{{ comparison.id }}</h1>
<p>{{ client.display_name }} — {{ policy.carrier_name }} {{ policy.policy_number }}</p>

<div class="cols">
  <div>
    <h2>Draft</h2>
    <form method="post" action="/comparisons/{{ comparison.id }}/draft">
      <textarea name="final_text" rows="14" style="width:100%; font: inherit;">{{
        (draft.final_text or draft.generated_text) if draft else "" }}</textarea>
      <p><button type="submit">Save edited draft</button></p>
    </form>
    <p class="source">
      Nothing is sent from here. Copy the text into your own email when you are
      happy with it.
    </p>

    <h2>Premium</h2>
    {% if breakdown.available %}
    <table>
      <tr><th>Line</th><th>Amount</th></tr>
      <tr><td>Total change</td><td>{{ breakdown.total_delta }}</td></tr>
      {% for line in breakdown.lines %}
      <tr><td>{{ line.label }}</td><td>{{ line.amount }}</td></tr>
      {% endfor %}
      <tr>
        <td>Not attributable from these documents</td>
        <td>{{ breakdown.residual }}</td>
      </tr>
    </table>
    {% else %}
    <p>Premium cannot be broken down: {{ breakdown.reason }}.</p>
    {% endif %}
  </div>

  <div>
    <h2>Differences</h2>
    <p>
      <a href="?show_noise={{ 0 if show_noise else 1 }}">
        {{ "hide noise" if show_noise else "show noise" }}
      </a>
    </p>
    <table>
      <tr><th>Field</th><th>Prior</th><th>Renewal</th><th>Class</th></tr>
      {% for difference in differences %}
      <tr>
        <td>{{ difference.field_path }}</td>
        <td>{{ difference.prior_value if difference.prior_value is not none
                else "—" }}</td>
        <td>{{ difference.renewal_value if difference.renewal_value is not none
                else "—" }}</td>
        <td>
          {{ difference.materiality }}
          <span class="source">({{ difference.rule_id }})</span>
          <form class="inline"
                action="/differences/{{ difference.id }}/reclassify" method="post">
            <select name="to_materiality">
              <option value="material">material</option>
              <option value="informational">informational</option>
              <option value="noise">noise</option>
            </select>
            <button type="submit">reclassify</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>
{% endblock %}
```

In `renewal/templates/run_review.html`, add this above the closing `{% endblock %}`:

```html
<h2>Promote and compare</h2>
{% if blocked %}
<p class="flag">
  These fields still need review: {{ blocked|join(", ") }}. Correct them, or tick
  them below to accept the extracted value as-is.
</p>
{% endif %}
<form method="post" action="/runs/{{ run.id }}/promote">
  {% for path in blocked %}
  <label>
    <input type="checkbox" name="acknowledged" value="{{ path }}"> {{ path }} is correct
  </label><br>
  {% endfor %}
  <p><button type="submit">Promote and compare</button></p>
</form>
```

- [x] **Step 4: Add the routes**

In `renewal/web.py`, extend the imports:

```python
from fastapi import Form
from renewal.comparison import breakdown_for, build_comparison, reclassify
from renewal.draft import generate_draft, latest_draft, save_edit
from renewal.materiality import load_rules
from renewal.models import Comparison, Difference, PolicyTerm
from renewal.promote import PromotionBlocked, promote, unresolved_field_paths
```

Replace the body of the existing `review` route with this version, which also
computes the fields still blocking promotion:

```python
    @app.get("/runs/{run_id}/review", response_class=HTMLResponse)
    def review(request: Request, run_id: int):
        session = db()
        run = session.get(RenewalRun, run_id)
        if run is None:
            session.close()
            raise HTTPException(status_code=404, detail="no such run")
        policy = session.get(Policy, run.policy_id)
        client_row = session.get(Client, policy.client_id)

        sides, extra_fields, blocked = [], {}, []
        for label, document_id in (
            ("Prior", run.prior_document_id),
            ("Renewal", run.renewal_document_id),
        ):
            extraction = _latest_extraction(session, document_id)
            values = effective_values(session, extraction.id)
            fields = (
                session.query(ExtractedField)
                .filter_by(extraction_id=extraction.id)
                .order_by(ExtractedField.field_path)
                .all()
            )
            for field in fields:
                field.effective_value = values.get(field.field_path, field.value)
            emitted = {field.field_path for field in fields}
            extra_fields[extraction.id] = [
                (path, value) for path, value in values.items() if path not in emitted
            ]
            blocked.extend(unresolved_field_paths(session, extraction.id))
            sides.append((label, extraction, fields))

        page = TEMPLATES.TemplateResponse(
            request,
            "run_review.html",
            {
                "run": run,
                "policy": policy,
                "client": client_row,
                "sides": sides,
                "extra_fields": extra_fields,
                "blocked": sorted(set(blocked)),
            },
        )
        session.close()
        return page
```

The template already refers to `client.display_name`, so rename the context key
back to `client` if you prefer — just keep the name consistent between the route
and `run_review.html`. Then add these routes before `return app`:

```python
    @app.post("/runs/{run_id}/promote")
    def promote_run(run_id: int, acknowledged: list[str] = Form(default=[])):
        session = db()
        run = session.get(RenewalRun, run_id)
        if run is None:
            session.close()
            raise HTTPException(status_code=404, detail="no such run")
        try:
            terms = [
                promote(
                    session,
                    _latest_extraction(session, document_id),
                    run.policy_id,
                    acknowledged=frozenset(acknowledged),
                )
                for document_id in (run.prior_document_id, run.renewal_document_id)
            ]
        except PromotionBlocked as blocked:
            session.rollback()
            session.close()
            raise HTTPException(
                status_code=400,
                detail=f"still needs review: {', '.join(blocked.paths)}",
            )

        rules = load_rules(settings.materiality_config)
        comparison = build_comparison(
            session,
            run_id=run.id,
            prior_term=terms[0],
            renewal_term=terms[1],
            rules=rules,
        )
        differences = (
            session.query(Difference).filter_by(comparison_id=comparison.id).all()
        )
        generate_draft(
            session,
            comparison,
            differences,
            breakdown_for(session, comparison),
            client=model_client,
            settings=settings,
        )
        session.commit()
        comparison_id = comparison.id
        session.close()
        return RedirectResponse(f"/comparisons/{comparison_id}", status_code=303)

    @app.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
    def show_comparison(request: Request, comparison_id: int, show_noise: int = 0):
        session = db()
        comparison = session.get(Comparison, comparison_id)
        if comparison is None:
            session.close()
            raise HTTPException(status_code=404, detail="no such comparison")
        term = session.get(PolicyTerm, comparison.prior_term_id)
        policy = session.get(Policy, term.policy_id)
        client_row = session.get(Client, policy.client_id)

        query = session.query(Difference).filter_by(comparison_id=comparison_id)
        if not show_noise:
            query = query.filter(Difference.materiality != "noise")
        differences = query.order_by(Difference.field_path).all()

        page = TEMPLATES.TemplateResponse(
            request,
            "comparison.html",
            {
                "comparison": comparison,
                "policy": policy,
                "client": client_row,
                "differences": differences,
                "breakdown": breakdown_for(session, comparison),
                "draft": latest_draft(session, comparison_id),
                "show_noise": bool(show_noise),
            },
        )
        session.close()
        return page

    @app.post("/comparisons/{comparison_id}/draft")
    def edit_draft(comparison_id: int, final_text: str = Form(...)):
        session = db()
        draft = latest_draft(session, comparison_id)
        if draft is None:
            session.close()
            raise HTTPException(status_code=404, detail="no draft yet")
        save_edit(session, draft, final_text)
        session.commit()
        session.close()
        return RedirectResponse(f"/comparisons/{comparison_id}", status_code=303)

    @app.post("/differences/{difference_id}/reclassify", status_code=204)
    def reclassify_difference(
        difference_id: int, to_materiality: str = Form(...), note: str | None = Form(None)
    ):
        session = db()
        difference = session.get(Difference, difference_id)
        if difference is None:
            session.close()
            raise HTTPException(status_code=404, detail="no such difference")
        reclassify(session, difference, to_materiality, note=note)
        session.commit()
        session.close()
        return Response(status_code=204)
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_comparison.py -v`
Expected: 6 passed

- [x] **Step 6: Run the whole suite — build-order steps 5 and 6 are complete**

Run: `pytest -v`
Expected: all passing.

- [ ] **Step 7: Use it on a real renewal**

Run the app, upload a real prior and renewal dec page, correct what is wrong,
promote, and read the draft against the diff table. Check every claim in the
draft against a source column. Note which differences were misclassified and
reclassify them — those rows are the first evidence about which rules are wrong.

- [x] **Step 8: Commit**

```bash
git add renewal/web.py renewal/templates/comparison.html \
        renewal/templates/run_review.html tests/test_web_comparison.py
git commit -m "feat: comparison screen with draft, diff table, and reclassification"
```

---

## Task 17: Scanned-document path

**Files:**
- Modify: `renewal/extract/runner.py` (only if the image branch needs fixing)
- Test: `tests/test_extract_scanned.py`

**Interfaces:**
- Consumes: `rasterize` (Task 3), `extract` (Task 7).
- Produces: no new interfaces. The image branch of `_build_content` already exists; this task proves it works end to end and fixes it if it does not.

- [x] **Step 1: Write the failing tests**

`tests/test_extract_scanned.py`:

```python
import base64
import json

import pytest

from renewal.config import Settings
from renewal.extract.runner import extract
from renewal.ingest import ingest_pdf
from tests.pdfmaker import make_scanned_pdf

LINES = [
    "PROGRESSIVE PERSONAL AUTO POLICY",
    "Policy Number AU-4471",
    "Total Policy Premium $1,840.00",
]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path,
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config=tmp_path / "materiality.yaml",
    )


class CapturingClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append(content)
        return self.response


def test_scanned_document_is_sent_as_page_images(session, store, settings):
    document = ingest_pdf(
        session,
        store,
        data=make_scanned_pdf([LINES, LINES]),
        original_filename="scan.pdf",
    )
    client = CapturingClient(json.dumps({"fields": []}))

    extract(session, store, document, "v1", client=client, settings=settings)

    content = client.calls[0]
    assert content[0]["type"] == "text"
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 2
    assert images[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(images[0]["source"]["data"]).startswith(b"\x89PNG")


def test_scanned_path_returns_the_same_structure_as_the_text_path(
    session, store, settings
):
    document = ingest_pdf(
        session, store, data=make_scanned_pdf([LINES]), original_filename="scan.pdf"
    )
    response = json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "1840.00",
                    "confidence": 0.88,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $1,840.00",
                }
            ]
        }
    )
    extraction = extract(
        session, store, document, "v1", client=CapturingClient(response),
        settings=settings,
    )
    field = extraction.fields[0]
    assert field.field_path == "policy.total_premium"
    assert field.source_page == 1
    assert field.source_text_span == "Total Policy Premium $1,840.00"


def test_source_text_cannot_be_verified_on_a_scanned_page(session, store, settings):
    """There is no text layer to check against, so every field is unverifiable
    and lands in review. That is the honest outcome, not a bug to paper over."""
    document = ingest_pdf(
        session, store, data=make_scanned_pdf([LINES]), original_filename="scan.pdf"
    )
    response = json.dumps(
        {
            "fields": [
                {
                    "field_path": "policy.total_premium",
                    "value": "1840.00",
                    "confidence": 0.88,
                    "source_page": 1,
                    "source_text": "Total Policy Premium $1,840.00",
                }
            ]
        }
    )
    extraction = extract(
        session, store, document, "v1", client=CapturingClient(response),
        settings=settings,
    )
    assert extraction.status == "partial"
    assert extraction.fields[0].needs_review is True
```

- [x] **Step 2: Run the tests**

Run: `pytest tests/test_extract_scanned.py -v`
Expected: the first two pass (the image branch already exists); the third
documents the real consequence of having no text layer. If any fail, fix
`_build_content` or `validate_fields` — do not weaken the source-text check to
make a test green.

- [ ] **Step 3: Confirm against a real scan**

Extract one genuinely scanned dec page through the UI. Every field will be
flagged `needs_review`, which means promotion forces a full read-through. Confirm
that is workable in practice; if it is not, that is a design conversation about
verification on scans, not a change to make quietly.

- [x] **Step 4: Commit — build-order step 7 is complete**

```bash
git add tests/test_extract_scanned.py
git commit -m "test: scanned-document extraction path"
```

---

## Done

All seven build-order steps from the spec are covered. The corpus now has:
a blob store that deduplicates, versioned extractions that can be re-run over
every document, a corrections table recording wrong values, omissions, and
hallucinations, frozen term snapshots, a rule-classified diff, an honest premium
breakdown with a stated residual, and a draft no one can send by accident.
