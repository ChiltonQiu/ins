# Record Layer, Plan A: Ingest Through Calendar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every document that enters the system is stored, text-extracted, attached to a client, and has its dates on a calendar she can confirm, dismiss, and subscribe to.

**Architecture:** One ingest entry point (`renewal/pipeline.py`) that all three intakes call. Stages are independently re-runnable and version-keyed, so a stage skips work it has already done and `bulk_import.py` can run stage-at-a-time across an archive without a job queue. Storage, text, dates, and search have no judgment in them; only structured field extraction does, and nothing here couples to it. Every table is insert-only — human judgment lands in event tables, latest event wins.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL (tsvector + pg_trgm), PyMuPDF, pytesseract + system Tesseract, cryptography (AES-GCM), pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-record-layer-design.md`

**Covers:** spec build-order steps 1–5. Plan B covers 6–7, Plan C covers 8–9.

## Global Constraints

- Every table is insert-only. Application code issues no UPDATE and no DELETE. Mutable state becomes an event table; current status is the latest event. (Spec D1)
- Logging carries ids, hashes, counts, and statuses only. Never document content, never field values, never page text.
- `unknown` is always an acceptable answer and is preferred to a confident wrong guess.
- Nothing acts automatically. Every extracted value is a suggestion a human confirms.
- Confidence values written by heuristic rules are labeled as heuristics in the code, not presented as probabilities.
- Test PDFs are synthetic, built with `tests/pdfmaker.py`. Never a real client document.
- `MIN_CHARS_FOR_TEXT_LAYER = 100` — the existing constant in `renewal/pdftext.py`, reused for the per-page text/OCR decision.
- Migrations are generated with `alembic revision -m "..."` so revision ids chain correctly; the current head is `66cc1100f4f8` (`0001_initial`). Never hand-write a revision id.
- One concern per commit. A migration is never committed with a feature.
- Existing behavior in the working slice does not change. `document.doc_type`, `document.has_text_layer`, and the structured extraction path are untouched.

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `renewal/crypto.py` | AES-GCM seal/unseal, magic-header detection |
| `renewal/pipeline.py` | The single ingest entry point; stage orchestration |
| `renewal/text/ocr.py` | Tesseract wrapper, one page in, text out |
| `renewal/text/store.py` | Per-page `document_text` rows, version-keyed skip logic |
| `renewal/resolve/candidates.py` | Pull named insured, policy numbers, address from page text |
| `renewal/resolve/matching.py` | Score and rank candidates against existing records |
| `renewal/resolve/service.py` | Auto-link or leave unmatched; manual assignment |
| `renewal/dates/regex_pass.py` | Free exhaustive date-literal sweep |
| `renewal/dates/prompt_v1.py` | Date-extraction prompt and JSON contract |
| `renewal/dates/llm_pass.py` | Bounded LLM pass over the first N pages |
| `renewal/dates/service.py` | Validation, insert, confirm/dismiss events |
| `renewal/calendarview/agenda.py` | Agenda query: dates + manual dates, escalation, filters |
| `renewal/calendarview/ics.py` | Read-only .ics rendering |
| `renewal/web/` | Router package; existing routes move here verbatim |
| `renewal/static/app.js` | Keystroke dismissal on the agenda |
| `scripts/bulk_import.py` | Stage-at-a-time archive walk, resumable |
| `scripts/encrypt_blobs.py` | Idempotent seal of an existing blob store |
| `scripts/redact.py` | Build synthetic fixtures from real documents |

**Modified**

| Path | Change |
|---|---|
| `renewal/models.py` | New tables; no existing model changes except two nullable columns on `Document` |
| `renewal/config.py` | `classification_model`, `date_model`, `date_pages`, `blob_encryption_key` |
| `renewal/blobstore.py` | Optional key, extension parameter |
| `renewal/web.py` | Deleted; contents move into `renewal/web/` |
| `evals/accuracy.py` | Date, classification, and client-match scoring |
| `conftest.py` | New tables in `TABLES`; `agency` fixture |
| `pyproject.toml` | New deps; `package-data` glob fix |
| `PRIVACY.md`, `.gitignore`, `README.md` | Per spec §Security |

---

## Task 1: Date scoring in the eval harness

Recall is the number that matters; a missed date is the failure with real consequences. Precision and recall are reported separately and only recall regression fails the build.

**Files:**
- Modify: `evals/accuracy.py`
- Test: `tests/test_eval_scoring.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `DateScore(tp: int, fp: int, fn: int, precision: float, recall: float)`, `score_dates(expected: list[dict], actual: list[dict], *, billing_type: str = "unknown") -> DateScore`. Both dict shapes are `{"date_value": "YYYY-MM-DD", "date_type": str}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eval_scoring.py
from evals.accuracy import DateScore, score_dates


def _d(value, type_):
    return {"date_value": value, "date_type": type_}


def test_exact_match_is_a_true_positive():
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "policy_expiration")])
    assert got == DateScore(tp=1, fp=0, fn=0, precision=1.0, recall=1.0)


def test_right_date_wrong_type_is_both_a_miss_and_a_false_positive():
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "renewal_due")])
    assert (got.tp, got.fp, got.fn) == (0, 1, 1)


def test_over_extraction_costs_precision_not_recall():
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "policy_expiration"),
                       _d("2026-01-01", "other")])
    assert got.recall == 1.0
    assert got.precision == 0.5


def test_duplicate_extractions_of_one_expected_date_count_once():
    """Both passes store their own row; the harness must not double-count."""
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "policy_expiration"),
                       _d("2026-07-01", "policy_expiration")])
    assert (got.tp, got.fp, got.fn) == (1, 0, 0)


def test_payment_due_is_not_scored_against_a_direct_bill_policy():
    """Spec: for direct-bill policies these dates are genuinely not in the
    documents she receives, so absence is not a recall failure."""
    got = score_dates([_d("2026-07-01", "payment_due")], [], billing_type="direct_bill")
    assert (got.tp, got.fp, got.fn) == (0, 0, 0)
    assert got.recall == 1.0


def test_payment_due_is_scored_against_an_agency_bill_policy():
    got = score_dates([_d("2026-07-01", "payment_due")], [], billing_type="agency_bill")
    assert got.fn == 1


def test_empty_expectation_has_perfect_recall():
    assert score_dates([], []).recall == 1.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_eval_scoring.py -v`
Expected: FAIL with `ImportError: cannot import name 'DateScore'`

- [ ] **Step 3: Implement the scoring**

```python
# evals/accuracy.py — append

@dataclass(frozen=True)
class DateScore:
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float


def _key(entry: dict) -> tuple[str, str]:
    return (entry["date_value"], entry["date_type"])


def score_dates(
    expected: list[dict], actual: list[dict], *, billing_type: str = "unknown"
) -> DateScore:
    """Precision and recall over (date_value, date_type) pairs.

    Both extraction passes store their own row for the same date, so actual is
    deduplicated before scoring: two rows for one real date is one hit, not a
    hit plus a false positive.

    payment_due is dropped entirely for a direct-bill policy. Those dates are
    not knowable from the documents she receives, so scoring them would chase
    recall on a field that genuinely is not there.
    """
    drop_payment_due = billing_type == "direct_bill"

    def keep(entry: dict) -> bool:
        return not (drop_payment_due and entry["date_type"] == "payment_due")

    want = {_key(e) for e in expected if keep(e)}
    got = {_key(a) for a in actual if keep(a)}
    tp = len(want & got)
    fp = len(got - want)
    fn = len(want - got)
    return DateScore(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=1.0 if not got else tp / len(got),
        recall=1.0 if not want else tp / len(want),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_eval_scoring.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add evals/accuracy.py tests/test_eval_scoring.py
git commit -m "feat(evals): score date extraction, recall separate from precision"
```

---

## Task 2: Classification and client-match scoring

`unknown` is a declined answer, not a wrong one. Scoring it as wrong would punish exactly the behavior the spec asks for.

**Files:**
- Modify: `evals/accuracy.py`
- Test: `tests/test_eval_scoring.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `score_classification(expected: str, actual: str) -> str` returning `"correct"`, `"declined"`, or `"wrong"`; `MatchScore(top1: bool, in_top_k: bool)`, `score_match(expected_client_id: int | None, ranked: list[int], k: int = 5) -> MatchScore`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eval_scoring.py — append
from evals.accuracy import MatchScore, score_classification, score_match


def test_correct_label():
    assert score_classification("declarations", "declarations") == "correct"


def test_unknown_is_declined_not_wrong():
    assert score_classification("declarations", "unknown") == "declined"


def test_confident_wrong_guess_is_wrong():
    assert score_classification("declarations", "invoice") == "wrong"


def test_expected_unknown_answered_unknown_is_correct():
    assert score_classification("unknown", "unknown") == "correct"


def test_top1_match():
    assert score_match(7, [7, 3, 9]) == MatchScore(top1=True, in_top_k=True)


def test_right_answer_offered_but_not_first():
    assert score_match(7, [3, 9, 7]) == MatchScore(top1=False, in_top_k=True)


def test_right_answer_never_offered():
    assert score_match(7, [3, 9]) == MatchScore(top1=False, in_top_k=False)


def test_no_expected_client_and_nothing_offered_is_a_hit():
    """A document that genuinely matches no client is correctly unmatched."""
    assert score_match(None, []) == MatchScore(top1=True, in_top_k=True)


def test_no_expected_client_but_something_offered_is_a_miss():
    assert score_match(None, [3]) == MatchScore(top1=False, in_top_k=False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_eval_scoring.py -v`
Expected: FAIL with `ImportError: cannot import name 'MatchScore'`

- [ ] **Step 3: Implement the scoring**

```python
# evals/accuracy.py — append

def score_classification(expected: str, actual: str) -> str:
    """'declined' is its own outcome. The spec prefers unknown to a confident
    wrong guess, so folding unknown into 'wrong' would score the harness
    against the behavior we want."""
    if actual == expected:
        return "correct"
    if actual == "unknown":
        return "declined"
    return "wrong"


@dataclass(frozen=True)
class MatchScore:
    top1: bool
    in_top_k: bool


def score_match(
    expected_client_id: int | None, ranked: list[int], k: int = 5
) -> MatchScore:
    """Top-1 and recall@k, so a wrong auto-link and a bad candidate list are
    distinguishable failures. expected None means the document should match no
    existing client, which offering nothing satisfies."""
    if expected_client_id is None:
        hit = not ranked
        return MatchScore(top1=hit, in_top_k=hit)
    return MatchScore(
        top1=bool(ranked) and ranked[0] == expected_client_id,
        in_top_k=expected_client_id in ranked[:k],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_eval_scoring.py -v`
Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add evals/accuracy.py tests/test_eval_scoring.py
git commit -m "feat(evals): score classification and client matching"
```

---

## Task 3: Extend the fixture format

Fixtures currently carry per-field ground truth only. They gain expected dates, an expected class, and an expected client so one fixture file drives every scorer.

**Files:**
- Modify: `evals/accuracy.py`, `evals/fixtures/progressive-auto-001.json`, `evals/fixtures/README.md`
- Test: `tests/test_eval_scoring.py`

**Interfaces:**
- Consumes: `Fixture` (existing).
- Produces: `Fixture` gains `dates: list[dict]`, `doc_class: str`, `expected_client: str | None`, `billing_type: str`. All four default so existing fixture files keep loading.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_eval_scoring.py — append
import json
from evals.accuracy import load_fixtures


def test_fixture_without_new_keys_still_loads(tmp_path):
    """Existing fixture files predate these keys and must not break."""
    (tmp_path / "a.json").write_text(json.dumps({
        "fixture_id": "a", "carrier": "Progressive",
        "pdf_filename": "a.pdf", "fields": {"policy.number": "X1"},
    }))
    fixture = load_fixtures(tmp_path)[0]
    assert fixture.dates == []
    assert fixture.doc_class == "unknown"
    assert fixture.expected_client is None
    assert fixture.billing_type == "unknown"


def test_fixture_with_new_keys_loads_them(tmp_path):
    (tmp_path / "b.json").write_text(json.dumps({
        "fixture_id": "b", "carrier": "Progressive",
        "pdf_filename": "b.pdf", "fields": {},
        "doc_class": "declarations",
        "expected_client": "Acme Landscaping LLC",
        "billing_type": "direct_bill",
        "dates": [{"date_value": "2026-07-01", "date_type": "policy_expiration"}],
    }))
    fixture = load_fixtures(tmp_path)[0]
    assert fixture.doc_class == "declarations"
    assert fixture.billing_type == "direct_bill"
    assert fixture.dates[0]["date_type"] == "policy_expiration"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_eval_scoring.py -k fixture -v`
Expected: FAIL with `AttributeError: 'Fixture' object has no attribute 'dates'`

- [ ] **Step 3: Extend the dataclass and loader**

```python
# evals/accuracy.py — replace the Fixture dataclass and load_fixtures

@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    carrier: str
    pdf_filename: str
    fields: dict[str, str]
    dates: list[dict] = field(default_factory=list)
    doc_class: str = "unknown"
    expected_client: str | None = None
    billing_type: str = "unknown"


def load_fixtures(directory: Path) -> list[Fixture]:
    """Keys added after the first fixtures were written all default, so an
    older fixture file stays valid rather than needing a bulk rewrite."""
    fixtures = []
    for path in sorted(Path(directory).glob("*.json")):
        data = json.loads(path.read_text())
        fixtures.append(
            Fixture(
                fixture_id=data["fixture_id"],
                carrier=data["carrier"],
                pdf_filename=data["pdf_filename"],
                fields=data["fields"],
                dates=data.get("dates", []),
                doc_class=data.get("doc_class", "unknown"),
                expected_client=data.get("expected_client"),
                billing_type=data.get("billing_type", "unknown"),
            )
        )
    return fixtures
```

Add `field` to the existing `from dataclasses import dataclass` import.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_eval_scoring.py -v`
Expected: 18 passed

- [ ] **Step 5: Document the new keys**

Append to `evals/fixtures/README.md`: each key, whether it is optional, and that `expected_client` is a display name resolved at eval time rather than an id, because ids differ between databases.

- [ ] **Step 6: Commit**

```bash
git add evals/accuracy.py evals/fixtures/README.md tests/test_eval_scoring.py
git commit -m "feat(evals): fixtures carry expected dates, class, and client"
```

---

## Task 4: Migration — agency, carrier, admitted status

**Files:**
- Modify: `renewal/models.py`, `conftest.py`
- Create: `migrations/versions/<generated>_agency_and_carrier.py`
- Test: `tests/test_models_record.py` (create)

**Interfaces:**
- Produces: models `Agency`, `Carrier`, `CarrierAlias`, `CarrierAdmittedStatus`; `Policy.state`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_record.py
from renewal.models import Agency, Carrier, CarrierAdmittedStatus, CarrierAlias


def test_agency_row_exists_with_a_seeded_default(session):
    agency = session.query(Agency).filter_by(slug="default").one()
    assert agency.ics_token
    assert len(agency.ics_token) >= 32


def test_admitted_status_is_keyed_per_state(session):
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    session.add(carrier)
    session.flush()
    session.add_all([
        CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                              status="non_admitted", set_by="human"),
        CarrierAdmittedStatus(carrier_id=carrier.id, state="AZ",
                              status="admitted", set_by="human"),
    ])
    session.flush()
    rows = {r.state: r.status for r in session.query(CarrierAdmittedStatus).all()}
    assert rows == {"CA": "non_admitted", "AZ": "admitted"}


def test_admitted_status_is_append_only_latest_wins(session):
    """Correcting a status inserts; it never updates."""
    carrier = Carrier(display_name="Travelers")
    session.add(carrier)
    session.flush()
    session.add(CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                                      status="unknown", set_by="human"))
    session.flush()
    session.add(CarrierAdmittedStatus(carrier_id=carrier.id, state="CA",
                                      status="admitted", set_by="human"))
    session.flush()
    rows = session.query(CarrierAdmittedStatus).order_by(
        CarrierAdmittedStatus.id).all()
    assert [r.status for r in rows] == ["unknown", "admitted"]


def test_carrier_alias_resolves_a_name_variant(session):
    carrier = Carrier(display_name="Progressive Casualty Ins Co")
    session.add(carrier)
    session.flush()
    session.add(CarrierAlias(carrier_id=carrier.id, alias="progressive"))
    session.flush()
    found = session.query(CarrierAlias).filter_by(alias="progressive").one()
    assert found.carrier_id == carrier.id
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_models_record.py -v`
Expected: FAIL with `ImportError: cannot import name 'Agency'`

- [ ] **Step 3: Add the models**

```python
# renewal/models.py — append

class Agency(Base):
    """One row. There is no auth and no tenancy; this exists so agency_id has
    a target and so the ics token and intake address have a home she can
    rotate from the UI."""

    __tablename__ = "agency"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    ics_token: Mapped[str] = mapped_column(Text, unique=True)
    intake_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Carrier(Base):
    __tablename__ = "carrier"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()


class CarrierAlias(Base):
    """carrier_name is free text on Policy and PolicyTerm, so name variants
    must resolve to one carrier. Exact normalized match only — a fuzzy carrier
    match that silently picked the wrong company would be invisible."""

    __tablename__ = "carrier_alias"
    id: Mapped[int] = mapped_column(primary_key=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey("carrier.id"))
    alias: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()


class CarrierAdmittedStatus(Base):
    """Per state: the same carrier can be admitted in one and surplus-lines in
    another. Human-set only, never inferred from a document. Append-only;
    the latest row per (carrier_id, state) wins."""

    __tablename__ = "carrier_admitted_status"
    __table_args__ = (
        CheckConstraint(
            "status IN ('admitted', 'non_admitted', 'unknown')",
            name="ck_carrier_admitted_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey("carrier.id"))
    state: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    set_by: Mapped[str] = mapped_column(Text, server_default="human")
    set_at: Mapped[datetime] = _created_at()
```

Add to `Policy`:

```python
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Generate and write the migration**

Run: `alembic revision -m "agency and carrier"`

In the generated file's `upgrade()`: create the four tables, add `policy.state`, create the index `ix_carrier_admitted_status_carrier_state` on `(carrier_id, state)`, and seed one agency row:

```python
    op.execute(
        "INSERT INTO agency (slug, display_name, ics_token) VALUES "
        "('default', 'Default Agency', encode(gen_random_bytes(32), 'hex'))"
    )
```

`gen_random_bytes` is in `pgcrypto`; add `op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")` above it. `downgrade()` drops the four tables and the column.

- [ ] **Step 5: Register the tables for truncation**

In `conftest.py`, add `carrier, carrier_alias, carrier_admitted_status` to the `TABLES` string.

**Do not add `agency`.** `clean_db` runs `TRUNCATE ... RESTART IDENTITY CASCADE`,
which would delete the row seeded by this migration and reset the sequence, so
every later test referencing `agency_id=1` would fail against a missing row.
The agency row is reference data, not test data. Add an assertion to
`clean_db` that it survives:

```python
    assert conn.execute(text("SELECT count(*) FROM agency")).scalar() == 1
```

so a future edit to `TABLES` fails loudly here instead of mysteriously later.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_models_record.py tests/test_models.py -v`
Expected: all pass. The session-scoped `engine` fixture drops and recreates the schema, so the new migration runs automatically.

- [ ] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/ conftest.py tests/test_models_record.py
git commit -m "feat(db): agency, carrier, and per-state admitted status"
```

---

## Task 5: Migration — billing type

Direct bill means payment lapses are invisible to her until the cancellation notice arrives, so this changes what she can see coming and belongs next to admitted status on the overview.

**Files:**
- Modify: `renewal/models.py`, `conftest.py`
- Create: `migrations/versions/<generated>_billing_type.py`
- Test: `tests/test_models_record.py`

**Interfaces:**
- Consumes: `Policy`, `PolicyTerm` (existing).
- Produces: model `PolicyBillingType`; `PolicyTerm.billing_type`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_record.py — append
from renewal.models import Client, Policy, PolicyBillingType, PolicyTerm


def _policy(session):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name="Travelers",
                    policy_number="P-1", line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    return policy


def test_billing_type_defaults_to_unknown_by_absence(session):
    """No row means unknown. unknown is preferable to a guess."""
    policy = _policy(session)
    assert session.query(PolicyBillingType).filter_by(policy_id=policy.id).count() == 0


def test_billing_type_is_append_only_latest_wins(session):
    policy = _policy(session)
    session.add(PolicyBillingType(policy_id=policy.id, billing_type="unknown",
                                  set_by="human"))
    session.flush()
    session.add(PolicyBillingType(policy_id=policy.id, billing_type="direct_bill",
                                  set_by="human"))
    session.flush()
    rows = session.query(PolicyBillingType).order_by(PolicyBillingType.id).all()
    assert [r.billing_type for r in rows] == ["unknown", "direct_bill"]


def test_policy_term_carries_the_extracted_billing_type(session):
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id, billing_type="agency_bill")
    session.add(term)
    session.flush()
    assert session.get(PolicyTerm, term.id).billing_type == "agency_bill"


def test_policy_term_billing_type_may_be_null(session):
    """Only declarations and endorsements get structured extraction, so most
    terms will never have a value here."""
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id)
    session.add(term)
    session.flush()
    assert session.get(PolicyTerm, term.id).billing_type is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_models_record.py -k billing -v`
Expected: FAIL with `ImportError: cannot import name 'PolicyBillingType'`

- [ ] **Step 3: Add the models**

```python
# renewal/models.py — append

class PolicyBillingType(Base):
    """Her authoritative value, set by hand. Append-only; latest per policy_id
    wins. PolicyTerm.billing_type holds what a dec page said, and the overview
    flags a disagreement — because a disagreement means billing changed at
    renewal, which is itself worth seeing."""

    __tablename__ = "policy_billing_type"
    __table_args__ = (
        CheckConstraint(
            "billing_type IN ('direct_bill', 'agency_bill', 'unknown')",
            name="ck_policy_billing_type",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    billing_type: Mapped[str] = mapped_column(Text)
    set_by: Mapped[str] = mapped_column(Text, server_default="human")
    set_at: Mapped[datetime] = _created_at()
```

Add to `PolicyTerm`:

```python
    billing_type: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Generate and write the migration**

Run: `alembic revision -m "billing type"`

`upgrade()` creates `policy_billing_type` with its check constraint, adds `policy_term.billing_type` as nullable text, and creates an index on `policy_billing_type(policy_id)`. `downgrade()` reverses both.

- [ ] **Step 5: Register the table for truncation**

Add `policy_billing_type` to the `TABLES` string in `conftest.py`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_models_record.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/ conftest.py tests/test_models_record.py
git commit -m "feat(db): billing type on the policy and on the term"
```

---

## Task 6: Migration — document text, classification, and document columns

The tsvector is a generated column so it can never drift from the text beside it. `to_tsvector('english', text)` with an explicit regconfig is immutable, which is what lets it be `GENERATED ALWAYS`.

**Files:**
- Modify: `renewal/models.py`, `conftest.py`
- Create: `migrations/versions/<generated>_document_text.py`
- Test: `tests/test_models_record.py`

**Interfaces:**
- Consumes: `Document` (existing).
- Produces: models `DocumentText`, `DocumentClassification`; `Document.source`, `Document.agency_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_record.py — append
from sqlalchemy import text as sql
from renewal.models import Document, DocumentClassification, DocumentText


def _document(session, agency_id=1):
    document = Document(blob_sha256="a" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=agency_id)
    session.add(document)
    session.flush()
    return document


def test_text_rows_are_unique_per_page_and_version(session):
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1,
                             text="hello", extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.flush()
    session.add(DocumentText(document_id=document.id, page_number=1,
                             text="hello again", extraction_method="pymupdf",
                             extractor_version="text-v1"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_page_may_be_re_extracted_at_a_new_version(session):
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1, text="a",
                             extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.add(DocumentText(document_id=document.id, page_number=1, text="b",
                             extraction_method="ocr_tesseract",
                             extractor_version="text-v2"))
    session.flush()
    assert session.query(DocumentText).count() == 2


def test_tsvector_is_populated_automatically(session):
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1,
                             text="cancellation effective July 2026",
                             extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.flush()
    found = session.execute(sql(
        "SELECT count(*) FROM document_text "
        "WHERE tsv @@ websearch_to_tsquery('english', 'cancellation')"
    )).scalar()
    assert found == 1


def test_an_empty_page_still_gets_a_row(session):
    """So the skip logic can tell 'processed, nothing there' from 'not yet
    processed'."""
    document = _document(session)
    session.add(DocumentText(document_id=document.id, page_number=1, text="",
                             extraction_method="pymupdf",
                             extractor_version="text-v1"))
    session.flush()
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1


def test_classification_is_appended_not_updated(session):
    document = _document(session)
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="unknown", confidence=0.2,
                                       classifier_version="classify-v1",
                                       model_id="anthropic:haiku"))
    session.flush()
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="cancellation_notice",
                                       confidence=0.9,
                                       classifier_version="classify-v2",
                                       model_id="anthropic:haiku"))
    session.flush()
    rows = session.query(DocumentClassification).order_by(
        DocumentClassification.id).all()
    assert [r.doc_class for r in rows] == ["unknown", "cancellation_notice"]
```

Add `import pytest` and `from sqlalchemy.exc import IntegrityError` at the top of the test file.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_models_record.py -k "text or classification" -v`
Expected: FAIL with `ImportError: cannot import name 'DocumentText'`

- [ ] **Step 3: Add the models**

```python
# renewal/models.py — append
from sqlalchemy import Computed, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR


class DocumentText(Base):
    """Every document, always. This path has no judgment in it: search and date
    extraction read from here and never depend on field-extraction accuracy."""

    __tablename__ = "document_text"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", "extractor_version",
                         name="uq_document_text_page_version"),
        Index("ix_document_text_tsv", "tsv", postgresql_using="gin"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    page_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(Text)
    extractor_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
    )


class DocumentClassification(Base):
    """A coarse, low-stakes label that drives routing and display only. It never
    gates storage, search, or date extraction. Latest row wins."""

    __tablename__ = "document_classification"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    doc_class: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    classifier_version: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
```

Add to `Document`:

```python
    source: Mapped[str] = mapped_column(Text, server_default="manual_upload")
    agency_id: Mapped[int | None] = mapped_column(
        ForeignKey("agency.id"), nullable=True
    )
```

`doc_class` is deliberately unconstrained at the database level: a check constraint would turn a future label into a migration, and the value is display-only. The allowed set lives in `renewal/classify/prompt_v1.py` and is asserted in that module's tests.

- [ ] **Step 4: Generate and write the migration**

Run: `alembic revision -m "document text and classification"`

`upgrade()` runs `op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")` — both search and client matching need it — then creates `document_text` (with the generated `tsv` column and its GIN index) and `document_classification`, adds `document.source` and `document.agency_id`, and backfills existing rows:

```python
    op.execute("UPDATE document SET agency_id = (SELECT id FROM agency "
               "WHERE slug = 'default')")
```

That backfill is the one UPDATE in the codebase. It runs once, in a migration, over rows that predate the column — it is not application code and does not weaken the invariant.

`downgrade()` drops both tables and both columns. It leaves the extensions in place; dropping a shared extension on downgrade would break unrelated indexes.

- [ ] **Step 5: Register the tables for truncation**

Add `document_text, document_classification` to the `TABLES` string in `conftest.py`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_models_record.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/ conftest.py tests/test_models_record.py
git commit -m "feat(db): per-page document text with a tsvector, and classification"
```

---

## Task 7: Migration — client resolution links

A document with no link row is unmatched. There is no status column and no queue table: the queue is the set of documents without a link.

**Files:**
- Modify: `renewal/models.py`, `conftest.py`
- Create: `migrations/versions/<generated>_document_link.py`
- Test: `tests/test_models_record.py`

**Interfaces:**
- Consumes: `Document`, `Client`, `Policy`.
- Produces: model `DocumentLink`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_record.py — append
from renewal.models import DocumentLink


def test_unmatched_is_the_absence_of_a_link(session):
    document = _document(session)
    assert session.query(DocumentLink).filter_by(document_id=document.id).count() == 0


def test_manual_assignment_appends_and_carries_the_candidates_offered(session):
    """The superseded auto row plus this candidates list is the Correction
    equivalent: what was offered, and what was right."""
    document = _document(session)
    client = Client(display_name="Acme Landscaping LLC")
    other = Client(display_name="Acme Landscaping Inc")
    session.add_all([client, other])
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=other.id,
                             method="auto", confidence=1.0, candidates=[]))
    session.flush()
    session.add(DocumentLink(
        document_id=document.id, client_id=client.id, method="manual",
        confidence=1.0,
        candidates=[{"client_id": other.id, "score": 0.91},
                    {"client_id": client.id, "score": 0.88}],
    ))
    session.flush()
    rows = session.query(DocumentLink).order_by(DocumentLink.id).all()
    assert [r.method for r in rows] == ["auto", "manual"]
    assert rows[-1].candidates[0]["score"] == 0.91


def test_a_link_may_have_no_policy(session):
    """She can know the client without knowing which policy the document is for."""
    document = _document(session)
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    link = DocumentLink(document_id=document.id, client_id=client.id,
                        policy_id=None, method="manual", confidence=1.0,
                        candidates=[])
    session.add(link)
    session.flush()
    assert session.get(DocumentLink, link.id).policy_id is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_models_record.py -k link -v`
Expected: FAIL with `ImportError: cannot import name 'DocumentLink'`

- [ ] **Step 3: Add the model**

```python
# renewal/models.py — append

class DocumentLink(Base):
    """Append-only; the latest row per document_id wins. A document with no row
    here is unmatched and appears in the queue — there is no status column to
    disagree with reality.

    candidates records the ranked list that was shown at the time. A manual row
    replacing an auto row is training data: these were offered, this was right.
    """

    __tablename__ = "document_link"
    __table_args__ = (
        CheckConstraint("method IN ('auto', 'manual')", name="ck_document_link_method"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy.id"), nullable=True
    )
    method: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    candidates: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = _created_at()
```

- [ ] **Step 4: Generate and write the migration**

Run: `alembic revision -m "document link"`

`upgrade()` creates the table with its check constraint and an index on `(document_id, id DESC)` — the latest-row-per-document lookup is the hot path for the calendar, the queue, and the overview. `downgrade()` drops it.

- [ ] **Step 5: Register the table for truncation**

Add `document_link` to the `TABLES` string in `conftest.py`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_models_record.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/ conftest.py tests/test_models_record.py
git commit -m "feat(db): append-only document to client links"
```

---

## Task 8: Migration — dates, date events, manual dates

`document_date` carries no `client_id`. Re-assigning a misfiled document must move every date on it to the right client with no backfill, so the client is derived through the document's latest link.

**Files:**
- Modify: `renewal/models.py`, `conftest.py`
- Create: `migrations/versions/<generated>_dates.py`
- Test: `tests/test_models_record.py`

**Interfaces:**
- Produces: models `DocumentDate`, `DateEvent`, `ManualDate`, `ManualDateEvent`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_record.py — append
from datetime import date
from renewal.models import DateEvent, DocumentDate, ManualDate, ManualDateEvent


def test_a_date_stores_its_provenance(session):
    document = _document(session)
    row = DocumentDate(
        document_id=document.id, date_value=date(2026, 7, 1),
        date_type="policy_expiration", source_page=2,
        source_text="Expiration Date: 07/01/2026", confidence=0.5,
        extractor_version="dates-regex-v1", pass_name="regex",
    )
    session.add(row)
    session.flush()
    stored = session.get(DocumentDate, row.id)
    assert stored.source_page == 2
    assert stored.source_text == "Expiration Date: 07/01/2026"


def test_a_date_has_no_client_id(session):
    """Derived through the document's latest link instead, so a re-link moves
    it with no backfill."""
    assert not hasattr(DocumentDate, "client_id")


def test_a_date_is_unconfirmed_until_an_event_says_otherwise(session):
    document = _document(session)
    row = DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                       date_type="other", source_page=1, source_text="07/01/2026",
                       confidence=0.3, extractor_version="dates-regex-v1",
                       pass_name="regex")
    session.add(row)
    session.flush()
    assert session.query(DateEvent).filter_by(document_date_id=row.id).count() == 0


def test_confirming_appends_an_event_and_leaves_the_date_untouched(session):
    document = _document(session)
    row = DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                       date_type="policy_expiration", source_page=1,
                       source_text="07/01/2026", confidence=0.5,
                       extractor_version="dates-regex-v1", pass_name="regex")
    session.add(row)
    session.flush()
    session.add(DateEvent(document_date_id=row.id, action="confirmed",
                          actor="human"))
    session.add(DateEvent(document_date_id=row.id, action="dismissed",
                          actor="human"))
    session.flush()
    events = session.query(DateEvent).order_by(DateEvent.id).all()
    assert [e.action for e in events] == ["confirmed", "dismissed"]


def test_a_derived_date_records_its_arithmetic(session):
    document = _document(session)
    row = DocumentDate(
        document_id=document.id, date_value=date(2026, 7, 1),
        date_type="cancellation_effective", source_page=1,
        source_text="within 30 days of the date of this notice",
        confidence=0.6, extractor_version="dates-llm-v1", pass_name="llm",
        is_derived=True, anchor_date=date(2026, 6, 1),
        anchor_source_text="Dated: June 1, 2026",
    )
    session.add(row)
    session.flush()
    stored = session.get(DocumentDate, row.id)
    assert stored.is_derived is True
    assert stored.anchor_date == date(2026, 6, 1)


def test_a_derived_date_may_have_an_unverified_anchor(session):
    """Stored and flagged rather than dropped; the UI warns instead."""
    document = _document(session)
    row = DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                       date_type="cancellation_effective", source_page=1,
                       source_text="within 30 days of this notice",
                       confidence=0.4, extractor_version="dates-llm-v1",
                       pass_name="llm", is_derived=True, anchor_date=None,
                       anchor_source_text=None)
    session.add(row)
    session.flush()
    assert session.get(DocumentDate, row.id).anchor_date is None


def test_a_manual_date_needs_no_document(session):
    """The only way this replaces the handwritten list."""
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    row = ManualDate(agency_id=1, client_id=client.id, title="Call about audit",
                     date_value=date(2026, 9, 1), date_type="audit_date",
                     created_by="human")
    session.add(row)
    session.flush()
    session.add(ManualDateEvent(manual_date_id=row.id, action="dismissed",
                                actor="human"))
    session.flush()
    assert session.query(ManualDateEvent).count() == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_models_record.py -k date -v`
Expected: FAIL with `ImportError: cannot import name 'DocumentDate'`

- [ ] **Step 3: Add the models**

```python
# renewal/models.py — append

DATE_TYPES = (
    "policy_effective", "policy_expiration", "renewal_due",
    "cancellation_effective", "non_renewal_effective", "payment_due",
    "inspection_deadline", "remediation_deadline", "audit_date", "other",
)
_DATE_TYPE_SQL = ", ".join(f"'{t}'" for t in DATE_TYPES)


class DocumentDate(Base):
    """An immutable extracted fact. Human judgment lands in DateEvent.

    No client_id: it is derived through the document's latest DocumentLink, so
    re-assigning a misfiled document moves every date on it with no backfill
    and no stale rows.

    is_derived marks a value the system calculated rather than read. That is
    the one place in this design where a rendered date was never printed on the
    document, and it can never be auto-confirmed.
    """

    __tablename__ = "document_date"
    __table_args__ = (
        CheckConstraint(f"date_type IN ({_DATE_TYPE_SQL})",
                        name="ck_document_date_type"),
        CheckConstraint("pass_name IN ('regex', 'llm')",
                        name="ck_document_date_pass"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    date_value: Mapped[date] = mapped_column(Date, index=True)
    date_type: Mapped[str] = mapped_column(Text)
    source_page: Mapped[int] = mapped_column(Integer)
    source_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    extractor_version: Mapped[str] = mapped_column(Text)
    pass_name: Mapped[str] = mapped_column("pass", Text)
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    anchor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    anchor_source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class DateEvent(Base):
    """'superseded' is written when a re-extraction at a newer version replaces
    a row she had already acted on, so her judgment is preserved rather than
    silently attached to a stale row."""

    __tablename__ = "date_event"
    __table_args__ = (
        CheckConstraint("action IN ('confirmed', 'dismissed', 'superseded')",
                        name="ck_date_event_action"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_date_id: Mapped[int] = mapped_column(
        ForeignKey("document_date.id"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, server_default="human")
    created_at: Mapped[datetime] = _created_at()


class ManualDate(Base):
    __tablename__ = "manual_date"
    __table_args__ = (
        CheckConstraint(f"date_type IN ({_DATE_TYPE_SQL})",
                        name="ck_manual_date_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agency.id"))
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("client.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text)
    date_value: Mapped[date] = mapped_column(Date, index=True)
    date_type: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, server_default="human")
    created_at: Mapped[datetime] = _created_at()


class ManualDateEvent(Base):
    __tablename__ = "manual_date_event"
    __table_args__ = (
        CheckConstraint("action IN ('confirmed', 'dismissed', 'superseded')",
                        name="ck_manual_date_event_action"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    manual_date_id: Mapped[int] = mapped_column(
        ForeignKey("manual_date.id"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, server_default="human")
    created_at: Mapped[datetime] = _created_at()
```

`pass` is a Python keyword, so the attribute is `pass_name` and the column is named `pass` explicitly. The spec's column name is preserved.

- [ ] **Step 4: Generate and write the migration**

Run: `alembic revision -m "dates and date events"`

`upgrade()` creates the four tables with their check constraints and the indexes declared above. `downgrade()` drops them in reverse dependency order.

- [ ] **Step 5: Register the tables for truncation**

Add `document_date, date_event, manual_date, manual_date_event` to the `TABLES` string in `conftest.py`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_models_record.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/ conftest.py tests/test_models_record.py
git commit -m "feat(db): document dates, date events, and manual dates"
```

---

## Task 9: Migration — inbound mail and attention queue

The tables land now, with the rest of the schema, so no later migration is mixed with a feature. Plan C builds what fills them.

**Files:**
- Modify: `renewal/models.py`, `conftest.py`
- Create: `migrations/versions/<generated>_mail_and_attention.py`
- Test: `tests/test_models_record.py`

**Interfaces:**
- Produces: models `InboundMessage`, `AttentionItem`, `AttentionEvent`; `Document.inbound_message_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_record.py — append
from datetime import datetime, timezone
from renewal.models import AttentionEvent, AttentionItem, InboundMessage


def test_message_id_is_unique_per_agency(session):
    """Forwarded mail arrives multiple times; the second arrival does no work."""
    for _ in range(2):
        session.add(InboundMessage(
            agency_id=1, message_id="<abc@carrier.example>",
            from_address="underwriting@carrier.example",
            to_address="intake+default@example.com", subject="Cancellation",
            received_at=datetime.now(timezone.utc),
            raw_mime_blob_sha256="b" * 64, body_text="",
            processing_status="received",
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_quarantined_mail_is_stored_not_dropped(session):
    message = InboundMessage(
        agency_id=1, message_id="<x@y>", from_address="stranger@example.com",
        to_address="intake+nobody@example.com", subject="hi",
        received_at=datetime.now(timezone.utc), raw_mime_blob_sha256="c" * 64,
        body_text="", processing_status="quarantined",
    )
    session.add(message)
    session.flush()
    assert session.get(InboundMessage, message.id).processing_status == "quarantined"


def test_attention_item_has_no_client_id(session):
    """Derived through the document's latest link, same as dates."""
    assert not hasattr(AttentionItem, "client_id")


def test_resolving_an_item_appends_an_event(session):
    document = _document(session)
    item = AttentionItem(document_id=document.id, reason_code="cancellation_notice",
                         reason_text="Classified as a cancellation notice")
    session.add(item)
    session.flush()
    session.add(AttentionEvent(attention_item_id=item.id, action="done",
                               actor="human"))
    session.flush()
    assert session.query(AttentionEvent).filter_by(action="done").count() == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_models_record.py -k "message or attention" -v`
Expected: FAIL with `ImportError: cannot import name 'InboundMessage'`

- [ ] **Step 3: Add the models**

```python
# renewal/models.py — append

class InboundMessage(Base):
    """The message body is stored as a document too, not only its attachments:
    the carrier's explanation is frequently in the body while the attachment is
    a bare form, and deadlines are very often stated in prose in the body."""

    __tablename__ = "inbound_message"
    __table_args__ = (
        UniqueConstraint("agency_id", "message_id", name="uq_inbound_message_id"),
        CheckConstraint(
            "processing_status IN ('received', 'processed', 'quarantined',"
            " 'duplicate', 'failed')",
            name="ck_inbound_message_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agency.id"))
    message_id: Mapped[str] = mapped_column(Text)
    from_address: Mapped[str] = mapped_column(Text)
    to_address: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_mime_blob_sha256: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class AttentionItem(Base):
    """Not a task manager: a list of documents that appear to need a human
    response, with a suggested reason. Never auto-resolves, never auto-acts."""

    __tablename__ = "attention_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    reason_code: Mapped[str] = mapped_column(Text)
    reason_text: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_at: Mapped[datetime] = _created_at()


class AttentionEvent(Base):
    __tablename__ = "attention_event"
    __table_args__ = (
        CheckConstraint("action IN ('done', 'dismissed')",
                        name="ck_attention_event_action"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    attention_item_id: Mapped[int] = mapped_column(
        ForeignKey("attention_item.id"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, server_default="human")
    created_at: Mapped[datetime] = _created_at()
```

Add to `Document`:

```python
    inbound_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_message.id"), nullable=True
    )
```

- [ ] **Step 4: Generate and write the migration**

Run: `alembic revision -m "inbound mail and attention"`

`upgrade()` creates the three tables, then adds `document.inbound_message_id`. The column is added here rather than in Task 6 because its target table does not exist until now. `downgrade()` drops the column first, then the tables.

- [ ] **Step 5: Register the tables for truncation**

Add `inbound_message, attention_item, attention_event` to the `TABLES` string in `conftest.py`.

- [ ] **Step 6: Verify the full migration chain from scratch**

Run: `pytest tests/ -v`
Expected: every existing test still passes and the new ones pass. The session-scoped `engine` fixture drops the schema and runs all seven migrations in order, so a broken chain fails here.

- [ ] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/ conftest.py tests/test_models_record.py
git commit -m "feat(db): inbound messages and the attention queue"
```

---

## Task 10: Blob encryption

Hash the plaintext so content addressing and dedup are unchanged; seal the bytes on disk. This defends against a stolen backup, a copied blob directory, and a decommissioned disk. It does **not** defend against a compromised host — the key sits next to the data.

**Files:**
- Create: `renewal/crypto.py`
- Modify: `pyproject.toml`
- Test: `tests/test_crypto.py` (create)

**Interfaces:**
- Produces: `MAGIC = b"RNB1"`, `seal(plaintext: bytes, key: bytes) -> bytes`, `unseal(blob: bytes, key: bytes) -> bytes`, `is_sealed(blob: bytes) -> bool`, `generate_key() -> str` (base64), `load_key(encoded: str | None) -> bytes | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crypto.py
import pytest

from renewal.crypto import (
    MAGIC, generate_key, is_sealed, load_key, seal, unseal,
)


def _key():
    return load_key(generate_key())


def test_round_trip():
    key = _key()
    assert unseal(seal(b"hello", key), key) == b"hello"


def test_sealed_bytes_do_not_contain_the_plaintext():
    key = _key()
    assert b"hello" not in seal(b"hello", key)


def test_sealed_bytes_carry_the_magic_header():
    assert seal(b"x", _key()).startswith(MAGIC)


def test_is_sealed_distinguishes_a_plain_pdf():
    assert not is_sealed(b"%PDF-1.7\n...")
    assert is_sealed(seal(b"%PDF-1.7\n...", _key()))


def test_two_seals_of_the_same_plaintext_differ():
    """A fresh nonce each time, so identical documents are not identifiable by
    their ciphertext on disk."""
    key = _key()
    assert seal(b"same", key) != seal(b"same", key)


def test_a_wrong_key_fails_loudly():
    sealed = seal(b"secret", _key())
    with pytest.raises(Exception):
        unseal(sealed, _key())


def test_tampered_ciphertext_fails_loudly():
    key = _key()
    sealed = bytearray(seal(b"secret", key))
    sealed[-1] ^= 0xFF
    with pytest.raises(Exception):
        unseal(bytes(sealed), key)


def test_load_key_of_none_is_none():
    assert load_key(None) is None
    assert load_key("") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_crypto.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.crypto'`

- [ ] **Step 3: Add the dependency**

Add `"cryptography>=42"` to `dependencies` in `pyproject.toml`, then `pip install -e .`

- [ ] **Step 4: Implement**

```python
# renewal/crypto.py
"""Encryption at rest for the blob store.

The plaintext is hashed, so content addressing and deduplication are unchanged;
only the bytes on disk are sealed. The magic header lets an existing store be
sealed in place idempotently.

Threat model, stated plainly because it is narrower than "encrypted" suggests:
this defends against a stolen backup, a copied blob directory, and a
decommissioned disk. It does not defend against a compromised host. The key
lives in the environment beside the data, so anything that can run the
application can read every document.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"RNB1"
NONCE_BYTES = 12
KEY_BYTES = 32


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def load_key(encoded: str | None) -> bytes | None:
    """None means the store runs unencrypted. That keeps local development and
    the eval harness working without key management; the application logs a
    warning at startup and PRIVACY.md says so rather than implying encryption
    is unconditional."""
    if not encoded:
        return None
    key = base64.b64decode(encoded)
    if len(key) != KEY_BYTES:
        raise ValueError(f"blob encryption key must be {KEY_BYTES} bytes")
    return key


def is_sealed(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def seal(plaintext: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def unseal(blob: bytes, key: bytes) -> bytes:
    if not is_sealed(blob):
        raise ValueError("blob is not sealed")
    start = len(MAGIC)
    nonce = blob[start : start + NONCE_BYTES]
    return AESGCM(key).decrypt(nonce, blob[start + NONCE_BYTES :], None)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_crypto.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add renewal/crypto.py tests/test_crypto.py pyproject.toml
git commit -m "feat(security): AES-GCM sealing for blobs at rest"
```

---

## Task 11: BlobStore takes a key and an extension

**Files:**
- Modify: `renewal/blobstore.py`, `renewal/config.py`, `.env.example`
- Test: `tests/test_blobstore.py`

**Interfaces:**
- Consumes: `renewal.crypto.seal`, `unseal`, `is_sealed`, `load_key`.
- Produces: `BlobStore(root: Path, key: bytes | None = None)`, `path_for(sha256: str, ext: str = "pdf") -> Path`, `put(data: bytes, ext: str = "pdf") -> str`, `get(sha256: str, ext: str = "pdf") -> bytes`. `Settings.blob_encryption_key: str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_blobstore.py — append
from renewal.crypto import generate_key, is_sealed, load_key


def test_hash_is_of_the_plaintext_so_dedup_is_unchanged(tmp_path):
    """The same document stored with and without a key gets the same digest."""
    plain = BlobStore(tmp_path / "a")
    sealed = BlobStore(tmp_path / "b", key=load_key(generate_key()))
    assert plain.put(b"%PDF-1.7 doc") == sealed.put(b"%PDF-1.7 doc")


def test_bytes_on_disk_are_sealed_when_a_key_is_set(tmp_path):
    store = BlobStore(tmp_path, key=load_key(generate_key()))
    digest = store.put(b"%PDF-1.7 doc")
    on_disk = store.path_for(digest).read_bytes()
    assert is_sealed(on_disk)
    assert b"%PDF-1.7 doc" not in on_disk


def test_get_returns_the_plaintext(tmp_path):
    store = BlobStore(tmp_path, key=load_key(generate_key()))
    digest = store.put(b"%PDF-1.7 doc")
    assert store.get(digest) == b"%PDF-1.7 doc"


def test_a_keyed_store_reads_a_blob_written_before_encryption(tmp_path):
    """Existing stores are not sealed yet; reads must keep working during the
    migration window."""
    unkeyed = BlobStore(tmp_path)
    digest = unkeyed.put(b"%PDF-1.7 legacy")
    keyed = BlobStore(tmp_path, key=load_key(generate_key()))
    assert keyed.get(digest) == b"%PDF-1.7 legacy"


def test_extension_selects_a_separate_file(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"raw mime here", ext="eml")
    assert store.path_for(digest, ext="eml").suffix == ".eml"
    assert store.get(digest, ext="eml") == b"raw mime here"


def test_default_extension_is_pdf_so_existing_paths_are_unchanged(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"%PDF-1.7")
    assert store.path_for(digest).suffix == ".pdf"


def test_a_bad_extension_is_rejected(tmp_path):
    """The extension reaches a filesystem path, so it is validated, not trusted."""
    store = BlobStore(tmp_path)
    with pytest.raises(ValueError):
        store.path_for("a" * 64, ext="../../etc/passwd")
```

Add `import pytest` at the top of the test file if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_blobstore.py -v`
Expected: FAIL with `TypeError: BlobStore.__init__() got an unexpected keyword argument 'key'`

- [ ] **Step 3: Implement**

```python
# renewal/blobstore.py — replace the class body, keeping the module docstring
import re
from pathlib import Path

from renewal.crypto import is_sealed, seal, unseal

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_EXT = re.compile(r"[a-z0-9]{1,8}")


class BlobStore:
    def __init__(self, root: Path, key: bytes | None = None) -> None:
        self.root = Path(root)
        self.key = key

    def path_for(self, sha256: str, ext: str = "pdf") -> Path:
        if not _SHA256_HEX.fullmatch(sha256):
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")
        if not _EXT.fullmatch(ext):
            raise ValueError(f"not a usable extension: {ext!r}")
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.{ext}"

    def put(self, data: bytes, ext: str = "pdf") -> str:
        """The digest is of the plaintext, so sealing changes nothing about
        content addressing or deduplication."""
        digest = hashlib.sha256(data).hexdigest()
        path = self.path_for(digest, ext)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = seal(data, self.key) if self.key else data
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        return digest

    def get(self, sha256: str, ext: str = "pdf") -> bytes:
        """Unsealed blobs still read back on a keyed store: an existing archive
        is sealed by scripts/encrypt_blobs.py, and reads must keep working
        while that runs."""
        path = self.path_for(sha256, ext)
        if not path.exists():
            raise BlobNotFound(sha256)
        raw = path.read_bytes()
        if self.key and is_sealed(raw):
            return unseal(raw, self.key)
        return raw
```

- [ ] **Step 4: Add the setting**

In `renewal/config.py`, add `blob_encryption_key: str = ""` to `Settings` and
`blob_encryption_key=os.environ.get("BLOB_ENCRYPTION_KEY", "")` to `load_settings()`.
Add a commented `BLOB_ENCRYPTION_KEY=` line to `.env.example` with a note that
`python -c "from renewal.crypto import generate_key; print(generate_key())"`
produces one, and that leaving it empty stores blobs unencrypted.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_blobstore.py tests/test_ingest.py -v`
Expected: all pass, including the existing blob tests unchanged

- [ ] **Step 6: Commit**

```bash
git add renewal/blobstore.py renewal/config.py .env.example tests/test_blobstore.py
git commit -m "feat(security): optional blob sealing and non-pdf blob extensions"
```

---

## Task 12: Seal an existing blob store

**Files:**
- Create: `scripts/encrypt_blobs.py`
- Test: `tests/test_encrypt_blobs.py` (create)

**Interfaces:**
- Consumes: `BlobStore`, `renewal.crypto`.
- Produces: `seal_store(root: Path, key: bytes) -> tuple[int, int]` returning `(sealed, already_sealed)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_encrypt_blobs.py
from renewal.blobstore import BlobStore
from renewal.crypto import generate_key, is_sealed, load_key
from scripts.encrypt_blobs import seal_store


def test_seals_existing_blobs_and_leaves_them_readable(tmp_path):
    store = BlobStore(tmp_path)
    digest = store.put(b"%PDF-1.7 legacy")
    key = load_key(generate_key())

    sealed, skipped = seal_store(tmp_path, key)

    assert (sealed, skipped) == (1, 0)
    assert is_sealed(store.path_for(digest).read_bytes())
    assert BlobStore(tmp_path, key=key).get(digest) == b"%PDF-1.7 legacy"


def test_is_idempotent(tmp_path):
    """Safe to re-run: the magic header says what is already done."""
    store = BlobStore(tmp_path)
    store.put(b"%PDF-1.7 legacy")
    key = load_key(generate_key())
    seal_store(tmp_path, key)
    assert seal_store(tmp_path, key) == (0, 1)


def test_the_digest_still_names_the_file(tmp_path):
    """Sealing must not change any blob's address."""
    store = BlobStore(tmp_path)
    digest = store.put(b"%PDF-1.7 legacy")
    seal_store(tmp_path, load_key(generate_key()))
    assert store.path_for(digest).exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_encrypt_blobs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.encrypt_blobs'`

- [ ] **Step 3: Implement**

```python
# scripts/encrypt_blobs.py
"""Seal an existing blob store in place. Idempotent: the magic header records
what is already done, so a re-run after an interrupted pass is free.

Writes through a temporary file and replaces atomically, so an interruption
leaves either the old blob or the new one, never a truncated file.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from renewal.config import load_settings
from renewal.crypto import is_sealed, load_key, seal

logger = logging.getLogger(__name__)


def seal_store(root: Path, key: bytes) -> tuple[int, int]:
    sealed = skipped = 0
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        raw = path.read_bytes()
        if is_sealed(raw):
            skipped += 1
            continue
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(seal(raw, key))
        tmp.replace(path)
        sealed += 1
        # Path only. Never the contents.
        logger.info("sealed %s", path.name)
    return sealed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()
    settings = load_settings()
    key = load_key(settings.blob_encryption_key)
    if key is None:
        raise SystemExit("BLOB_ENCRYPTION_KEY is not set; nothing to do")
    sealed, skipped = seal_store(args.root or settings.blob_root, key)
    print(f"sealed {sealed}, already sealed {skipped}")


if __name__ == "__main__":
    main()
```

Create `scripts/__init__.py` (empty) so the test can import it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_encrypt_blobs.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/encrypt_blobs.py scripts/__init__.py tests/test_encrypt_blobs.py
git commit -m "feat(security): idempotent sealing of an existing blob store"
```

---

## Task 13: OCR a page with Tesseract

Local only. No scan is ever transmitted for text extraction; the structured extractor keeps its existing vision path and is not touched.

**Files:**
- Create: `renewal/text/__init__.py`, `renewal/text/ocr.py`
- Modify: `renewal/pdftext.py`, `pyproject.toml`, `README.md`
- Test: `tests/test_ocr.py` (create)

**Interfaces:**
- Consumes: `renewal.pdftext`.
- Produces: `rasterize_page(data: bytes, page_number: int, dpi: int = 300) -> bytes` in `pdftext`; `ocr_page(png: bytes) -> str` and `TesseractUnavailable` in `text/ocr.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ocr.py
import pytest

from renewal.pdftext import rasterize_page, read_pdf
from renewal.text.ocr import TesseractUnavailable, ocr_page
from tests.pdfmaker import make_scanned_pdf, make_text_pdf

tesseract = pytest.importorskip("pytesseract")


def test_rasterize_page_returns_one_page(tmp_path):
    data = make_text_pdf([["page one"], ["page two"]])
    png = rasterize_page(data, 2)
    assert png.startswith(b"\x89PNG")


def test_rasterize_page_is_one_based_and_range_checked():
    data = make_text_pdf([["only page"]])
    with pytest.raises(ValueError):
        rasterize_page(data, 0)
    with pytest.raises(ValueError):
        rasterize_page(data, 2)


@pytest.mark.ocr
def test_ocr_recovers_text_from_a_scanned_page():
    """The whole point: a scanned page has no text layer, and search still
    has to find it."""
    data = make_scanned_pdf([["CANCELLATION NOTICE"]])
    assert not read_pdf(data).has_text_layer
    text = ocr_page(rasterize_page(data, 1))
    assert "CANCELLATION" in text.upper()


@pytest.mark.ocr
def test_ocr_of_a_blank_page_is_empty_not_an_error():
    data = make_scanned_pdf([[]])
    assert ocr_page(rasterize_page(data, 1)).strip() == ""
```

- [ ] **Step 2: Register the marker and run the test to verify it fails**

Add `"ocr: needs the system tesseract binary"` to `markers` in `pyproject.toml`, and change `addopts` to `-m 'not eval'` (unchanged — `ocr` tests run by default, because a machine that cannot OCR cannot run the import path and should fail loudly).

Run: `pytest tests/test_ocr.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.text'`

- [ ] **Step 3: Add the dependency**

Add `"pytesseract>=0.3.10"` to `dependencies` in `pyproject.toml` and run `pip install -e .`. Install the system binary: `sudo pacman -S tesseract tesseract-data-eng` on Arch, `apt install tesseract-ocr` on Debian. Document both in `README.md` under a "System dependencies" heading, noting that without it scanned documents are stored and searchable only by filename.

- [ ] **Step 4: Implement**

```python
# renewal/pdftext.py — append

def rasterize_page(data: bytes, page_number: int, dpi: int = 300) -> bytes:
    """One page, 1-based, at OCR resolution.

    300 dpi rather than the 200 used for the vision model: Tesseract's accuracy
    on small print falls off below that, and the pixmap is discarded
    immediately either way.
    """
    with fitz.open(stream=data, filetype="pdf") as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"page {page_number} out of range")
        return doc[page_number - 1].get_pixmap(dpi=dpi).tobytes("png")
```

```python
# renewal/text/ocr.py
"""Optical character recognition, locally.

Tesseract runs on this machine. No scanned page is transmitted to a model
provider for text extraction, which keeps the always-on ingest path free of
third-party exposure however PROVIDER is set.

Tesseract is weaker than a vision model on faxed and skewed pages. That is
accepted: this path feeds search recall and date-shaped strings, not structured
field accuracy.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


class TesseractUnavailable(RuntimeError):
    """The system binary is missing. Raised rather than silently returning an
    empty string, which would record a scanned page as legitimately blank."""


def ocr_page(png: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment problem
        raise TesseractUnavailable(str(exc)) from exc

    try:
        with Image.open(io.BytesIO(png)) as image:
            return pytesseract.image_to_string(image)
    except pytesseract.TesseractNotFoundError as exc:
        raise TesseractUnavailable("tesseract binary not on PATH") from exc
```

`pytesseract` pulls in Pillow, so no separate dependency is needed.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_ocr.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add renewal/text/ renewal/pdftext.py pyproject.toml README.md tests/test_ocr.py
git commit -m "feat(text): local tesseract OCR and single-page rasterization"
```

---

## Task 14: Per-page text extraction

Every document, always. The decision is per page, not per document: a scanned endorsement stapled to a digital dec page gets both methods, and the row records which.

**Files:**
- Create: `renewal/text/store.py`
- Test: `tests/test_text_store.py` (create)

**Interfaces:**
- Consumes: `BlobStore`, `renewal.pdftext.read_pdf`, `rasterize_page`, `renewal.text.ocr.ocr_page`, model `DocumentText`.
- Produces: `TEXT_VERSION = "text-v1"`, `extract_text(session, store, document, *, version=TEXT_VERSION) -> list[DocumentText]`, `has_text(session, document_id: int, version: str = TEXT_VERSION) -> bool`, `page_text(session, document_id: int, page_number: int, version: str = TEXT_VERSION) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_text_store.py
import pytest

from renewal.ingest import ingest_pdf
from renewal.models import DocumentText
from renewal.text.store import TEXT_VERSION, extract_text, has_text, page_text
from tests.pdfmaker import make_scanned_pdf, make_text_pdf


def _ingest(session, store, data):
    return ingest_pdf(session, store, data=data, original_filename="d.pdf")


def test_a_text_layer_page_uses_pymupdf(session, store):
    document = _ingest(session, store, make_text_pdf([["Named Insured: Acme"]]))
    rows = extract_text(session, store, document)
    assert [r.extraction_method for r in rows] == ["pymupdf"]
    assert "Acme" in rows[0].text


def test_page_numbers_are_one_based(session, store):
    document = _ingest(session, store, make_text_pdf([["first"], ["second"]]))
    rows = extract_text(session, store, document)
    assert [r.page_number for r in rows] == [1, 2]
    assert "second" in page_text(session, document.id, 2)


@pytest.mark.ocr
def test_a_scanned_page_falls_back_to_ocr(session, store):
    document = _ingest(session, store, make_scanned_pdf([["CANCELLATION NOTICE"]]))
    rows = extract_text(session, store, document)
    assert rows[0].extraction_method == "ocr_tesseract"
    assert "CANCELLATION" in rows[0].text.upper()


@pytest.mark.ocr
def test_the_method_is_decided_per_page_not_per_document(session, store):
    """A digital dec page with a scanned endorsement behind it."""
    digital = make_text_pdf([["Named Insured: Acme Landscaping LLC"]])
    scanned = make_scanned_pdf([["ENDORSEMENT"]])
    import fitz
    merged = fitz.open(stream=digital, filetype="pdf")
    merged.insert_pdf(fitz.open(stream=scanned, filetype="pdf"))
    document = _ingest(session, store, merged.tobytes())
    rows = extract_text(session, store, document)
    assert [r.extraction_method for r in rows] == ["pymupdf", "ocr_tesseract"]


def test_an_empty_page_still_gets_a_row(session, store):
    document = _ingest(session, store, make_text_pdf([[]]))
    rows = extract_text(session, store, document)
    assert len(rows) == 1
    assert has_text(session, document.id) is True


def test_re_running_at_the_same_version_is_a_no_op(session, store):
    document = _ingest(session, store, make_text_pdf([["hello"]]))
    extract_text(session, store, document)
    extract_text(session, store, document)
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1


def test_has_text_is_false_before_extraction(session, store):
    document = _ingest(session, store, make_text_pdf([["hello"]]))
    assert has_text(session, document.id) is False


def test_extraction_at_a_new_version_adds_rows_rather_than_replacing(session, store):
    document = _ingest(session, store, make_text_pdf([["hello"]]))
    extract_text(session, store, document)
    extract_text(session, store, document, version="text-v2")
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_text_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.text.store'`

- [ ] **Step 3: Implement**

```python
# renewal/text/store.py
"""Per-page text for every document.

This path has no judgment in it and cannot be silently wrong in a harmful way.
Search and date extraction read from here; neither depends on field-extraction
accuracy, and nothing in this module may couple them.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.models import Document, DocumentText
from renewal.pdftext import MIN_CHARS_FOR_TEXT_LAYER, rasterize_page, read_pdf
from renewal.text.ocr import ocr_page

logger = logging.getLogger(__name__)

TEXT_VERSION = "text-v1"


def has_text(session: Session, document_id: int, version: str = TEXT_VERSION) -> bool:
    return session.scalar(
        select(DocumentText.id)
        .where(DocumentText.document_id == document_id)
        .where(DocumentText.extractor_version == version)
        .limit(1)
    ) is not None


def page_text(
    session: Session, document_id: int, page_number: int, version: str = TEXT_VERSION
) -> str:
    return session.scalar(
        select(DocumentText.text)
        .where(DocumentText.document_id == document_id)
        .where(DocumentText.page_number == page_number)
        .where(DocumentText.extractor_version == version)
    ) or ""


def extract_text(
    session: Session,
    store: BlobStore,
    document: Document,
    *,
    version: str = TEXT_VERSION,
) -> list[DocumentText]:
    """Idempotent at a given version, so the bulk-import stage can be re-run
    over the whole archive without duplicating work.

    The method is decided per page: a PDF can carry a digital dec page and a
    scanned endorsement, and search quality varies by which produced the text.
    """
    if has_text(session, document.id, version):
        return list(
            session.scalars(
                select(DocumentText)
                .where(DocumentText.document_id == document.id)
                .where(DocumentText.extractor_version == version)
                .order_by(DocumentText.page_number)
            )
        )

    data = store.get(document.blob_sha256)
    pdf = read_pdf(data)
    rows: list[DocumentText] = []
    for page in pdf.pages:
        text = page.text
        method = "pymupdf"
        if len("".join(text.split())) < MIN_CHARS_FOR_TEXT_LAYER:
            text = ocr_page(rasterize_page(data, page.page_number))
            method = "ocr_tesseract"
        row = DocumentText(
            document_id=document.id,
            page_number=page.page_number,
            text=text,
            extraction_method=method,
            extractor_version=version,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    # Counts and methods only. Never the text.
    logger.info(
        "text extracted document_id=%s sha256=%s pages=%s methods=%s version=%s",
        document.id,
        document.blob_sha256,
        len(rows),
        sorted({r.extraction_method for r in rows}),
        version,
    )
    return rows
```

An empty page keeps its `pymupdf` row only when it clears the threshold; a
blank page falls to OCR, which returns an empty string, and the row is written
either way. That is deliberate: a row means "processed", and its absence means
"not yet processed".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_text_store.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/text/store.py tests/test_text_store.py
git commit -m "feat(text): per-page text extraction with per-page method choice"
```

---

## Task 15: The ingest pipeline

One entry point. Bulk import, manual upload, and email intake all call it. Stages are separately re-runnable and version-keyed, so a stage skips a document that already has output.

At this task the pipeline runs stages 1–3. Later tasks add stages 4 and 5; Plan C adds 6–8. Each addition is a stage function registered in one list, so nothing else changes.

**Files:**
- Create: `renewal/pipeline.py`
- Test: `tests/test_pipeline.py` (create)

**Interfaces:**
- Consumes: `ingest_pdf`, `extract_text`, `has_text`.
- Produces: `SOURCES = ("bulk_import", "manual_upload", "email_attachment", "email_body")`, `ingest_document(session, store, *, data, original_filename, source, agency_id, inbound_message_id=None) -> Document`, `run_text_stage(session, store, document) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py
import pytest

from renewal.models import Document, DocumentText
from renewal.pipeline import ingest_document
from tests.pdfmaker import make_text_pdf


def test_ingest_stores_the_document_and_its_text(session, store):
    document = ingest_document(
        session, store, data=make_text_pdf([["Named Insured: Acme"]]),
        original_filename="dec.pdf", source="bulk_import", agency_id=1,
    )
    assert document.source == "bulk_import"
    assert document.agency_id == 1
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 1


def test_identical_bytes_deduplicate_the_blob_but_not_the_document(session, store):
    """The same PDF arriving twice is two events, one blob."""
    data = make_text_pdf([["Named Insured: Acme"]])
    first = ingest_document(session, store, data=data, original_filename="a.pdf",
                            source="bulk_import", agency_id=1)
    second = ingest_document(session, store, data=data, original_filename="b.pdf",
                             source="email_attachment", agency_id=1)
    assert first.id != second.id
    assert first.blob_sha256 == second.blob_sha256


def test_an_unknown_source_is_rejected(session, store):
    with pytest.raises(ValueError):
        ingest_document(session, store, data=make_text_pdf([["x"]]),
                        original_filename="a.pdf", source="telepathy", agency_id=1)


def test_text_extraction_failure_does_not_lose_the_document(session, store, monkeypatch):
    """Storage must never depend on a later stage succeeding."""
    import renewal.pipeline as pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("tesseract exploded")

    monkeypatch.setattr(pipeline, "extract_text", boom)
    document = ingest_document(session, store, data=make_text_pdf([["x"]]),
                               original_filename="a.pdf", source="bulk_import",
                               agency_id=1)
    assert session.get(Document, document.id) is not None
    assert session.query(DocumentText).filter_by(document_id=document.id).count() == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.pipeline'`

- [ ] **Step 3: Implement**

```python
# renewal/pipeline.py
"""The one way a document enters the system.

Bulk import, manual upload, and email intake all call ingest_document. Every
document is stored, text-extracted, date-extracted, and made searchable —
those stages have no judgment in them. Only some documents go on to structured
field extraction, and only high-confidence extractions are promoted. Search and
the calendar never depend on field-extraction accuracy.

Every stage after storage is best-effort and separately re-runnable. A stage
that raises is logged and skipped: losing the document because OCR failed would
be worse than a document with no text yet, and the stage can be re-run.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.ingest import ingest_pdf
from renewal.models import Document
from renewal.text.store import extract_text, has_text

logger = logging.getLogger(__name__)

SOURCES = ("bulk_import", "manual_upload", "email_attachment", "email_body")


def run_text_stage(session: Session, store: BlobStore, document: Document) -> None:
    if has_text(session, document.id):
        return
    try:
        extract_text(session, store, document)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception(
            "text stage failed document_id=%s sha256=%s",
            document.id,
            document.blob_sha256,
        )


def ingest_document(
    session: Session,
    store: BlobStore,
    *,
    data: bytes,
    original_filename: str,
    source: str,
    agency_id: int,
    inbound_message_id: int | None = None,
) -> Document:
    if source not in SOURCES:
        raise ValueError(f"unknown document source: {source!r}")
    document = ingest_pdf(
        session, store, data=data, original_filename=original_filename
    )
    document.source = source
    document.agency_id = agency_id
    document.inbound_message_id = inbound_message_id
    session.flush()
    run_text_stage(session, store, document)
    return document
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_pipeline.py tests/test_ingest.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add renewal/pipeline.py tests/test_pipeline.py
git commit -m "feat(ingest): one pipeline entry point for every intake"
```

---

## Task 16: Bulk import

Import is cheap and cannot be done retroactively for documents that get deleted later. Get the whole archive in early even if extraction is still weak — extraction is a pure function of (blob, extractor_version), so everything can be re-extracted.

**Files:**
- Create: `scripts/bulk_import.py`
- Modify: `.gitignore`
- Test: `tests/test_bulk_import.py` (create)

**Interfaces:**
- Consumes: `ingest_document`, `run_text_stage`, `BlobStore`, `session_scope`.
- Produces: `walk_pdfs(root: Path) -> Iterator[Path]`, `import_tree(session, store, root: Path, *, agency_id: int) -> ImportStats`, `ImportStats(seen, imported, skipped, failed)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bulk_import.py
from renewal.models import Document, DocumentText
from scripts.bulk_import import ImportStats, import_tree, walk_pdfs
from tests.pdfmaker import make_text_pdf


def _tree(tmp_path):
    (tmp_path / "2024" / "acme").mkdir(parents=True)
    (tmp_path / "2024" / "acme" / "dec.pdf").write_bytes(
        make_text_pdf([["Named Insured: Acme Landscaping LLC"]]))
    (tmp_path / "2024" / "notes.txt").write_text("not a pdf")
    (tmp_path / "2025").mkdir()
    (tmp_path / "2025" / "renewal.PDF").write_bytes(
        make_text_pdf([["Named Insured: Acme Landscaping LLC"]]))
    return tmp_path


def test_walk_finds_pdfs_recursively_and_case_insensitively(tmp_path):
    root = _tree(tmp_path)
    names = sorted(p.name for p in walk_pdfs(root))
    assert names == ["dec.pdf", "renewal.PDF"]


def test_import_stores_every_pdf_with_its_text(session, store, tmp_path):
    stats = import_tree(session, store, _tree(tmp_path), agency_id=1)
    assert stats.imported == 2
    assert session.query(Document).count() == 2
    assert session.query(DocumentText).count() == 2


def test_re_running_is_free(session, store, tmp_path):
    """Content addressing makes a re-import free; the second pass imports
    nothing and re-extracts nothing."""
    root = _tree(tmp_path)
    import_tree(session, store, root, agency_id=1)
    second = import_tree(session, store, root, agency_id=1)
    assert second.imported == 0
    assert second.skipped == 2
    assert session.query(Document).count() == 2


def test_an_unreadable_pdf_is_counted_and_does_not_stop_the_walk(
    session, store, tmp_path
):
    root = _tree(tmp_path)
    (root / "broken.pdf").write_bytes(b"not really a pdf")
    stats = import_tree(session, store, root, agency_id=1)
    assert stats.failed == 1
    assert stats.imported == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_bulk_import.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.bulk_import'`

- [ ] **Step 3: Implement**

```python
# scripts/bulk_import.py
"""Walk a directory tree and put every PDF through the ingest pipeline.

Safely re-runnable. Content addressing makes a re-import free: a blob already
in the store is not rewritten, and a document whose bytes are already known at
this path is skipped rather than duplicated.

One document failing never stops the walk. Import is cheap and cannot be done
retroactively for a document that is later deleted from the source tree, so
getting the archive in matters more than getting every page perfect on the
first pass — extraction is a pure function of (blob, extractor_version) and
can be re-run.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.crypto import load_key
from renewal.db import session_scope
from renewal.models import Document
from renewal.pipeline import ingest_document, run_text_stage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportStats:
    seen: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0


def walk_pdfs(root: Path) -> Iterator[Path]:
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.suffix.lower() == ".pdf":
            yield path


def import_tree(
    session: Session, store: BlobStore, root: Path, *, agency_id: int
) -> ImportStats:
    seen = imported = skipped = failed = 0
    for path in walk_pdfs(root):
        seen += 1
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            existing = session.scalar(
                select(Document.id).where(Document.blob_sha256 == digest).limit(1)
            )
            if existing is not None:
                # Already imported. Re-run the text stage in case it is behind
                # the current version; it is a no-op when it is not.
                run_text_stage(session, store, session.get(Document, existing))
                skipped += 1
                continue
            ingest_document(
                session, store, data=data, original_filename=path.name,
                source="bulk_import", agency_id=agency_id,
            )
            imported += 1
        except Exception:  # noqa: BLE001 - one bad file must not stop the archive
            failed += 1
            # Path only. Never the contents.
            logger.exception("import failed path=%s", path)
    return ImportStats(seen=seen, imported=imported, skipped=skipped, failed=failed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--agency-id", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    store = BlobStore(settings.blob_root, key=load_key(settings.blob_encryption_key))
    with session_scope() as session:
        stats = import_tree(session, store, args.root, agency_id=args.agency_id)
    print(f"seen {stats.seen}, imported {stats.imported}, "
          f"skipped {stats.skipped}, failed {stats.failed}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Ignore import staging**

Add to `.gitignore`:

```
import/
mime/
*.eml
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_bulk_import.py -v`
Expected: 4 passed

- [ ] **Step 6: Verify against a real tree**

Run: `python -m scripts.bulk_import /path/to/a/small/sample/folder`
Expected: a count line, and `psql renewal -c "select count(*) from document_text"` returns a non-zero row count. Confirm nothing in the log output contains document text.

- [ ] **Step 7: Commit**

```bash
git add scripts/bulk_import.py .gitignore tests/test_bulk_import.py
git commit -m "feat(ingest): resumable bulk import of a PDF archive"
```

---

## Task 17: Candidate identifiers and ranked matches

Return ranked candidates with scores, never a single silent answer. This is the part that will be wrong most often, so it is human-assisted from the start.

**Files:**
- Create: `renewal/resolve/__init__.py`, `renewal/resolve/candidates.py`, `renewal/resolve/matching.py`
- Test: `tests/test_resolve.py` (create)

**Interfaces:**
- Consumes: models `Client`, `Policy`; `pg_trgm`.
- Produces: `Candidates(named_insured: str | None, policy_numbers: list[str], address_lines: list[str])`, `extract_candidates(page_text: str) -> Candidates`; `Match(client_id: int, policy_id: int | None, score: float, reason: str)`, `rank_matches(session, candidates: Candidates, *, limit: int = 5) -> list[Match]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve.py
from renewal.models import Client, Policy
from renewal.resolve.candidates import extract_candidates
from renewal.resolve.matching import rank_matches

DEC = """\
DECLARATIONS PAGE
Named Insured: Acme Landscaping LLC
Mailing Address: 1400 Harbor Blvd
Fullerton, CA 92832
Policy Number: CAP-7781-22
Effective Date: 07/01/2025
"""


def test_named_insured_is_read_from_its_label():
    assert extract_candidates(DEC).named_insured == "Acme Landscaping LLC"


def test_policy_number_is_read_from_its_label():
    assert "CAP-7781-22" in extract_candidates(DEC).policy_numbers


def test_address_lines_are_captured():
    lines = extract_candidates(DEC).address_lines
    assert any("Fullerton" in line for line in lines)


def test_missing_labels_yield_nothing_rather_than_a_guess():
    got = extract_candidates("A letter with no labels at all.")
    assert got.named_insured is None
    assert got.policy_numbers == []


def _seed(session):
    acme = Client(display_name="Acme Landscaping LLC")
    other = Client(display_name="Acme Landscaping Inc")
    session.add_all([acme, other])
    session.flush()
    policy = Policy(client_id=acme.id, carrier_name="Travelers",
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    return acme, other, policy


def test_an_exact_policy_number_scores_one(session):
    acme, _, policy = _seed(session)
    matches = rank_matches(session, extract_candidates(DEC))
    assert matches[0].score == 1.0
    assert matches[0].client_id == acme.id
    assert matches[0].policy_id == policy.id
    assert matches[0].reason == "exact policy number"


def test_a_similar_name_ranks_but_never_scores_one(session):
    """Name similarity alone must never reach the auto-link threshold."""
    _seed(session)
    text = DEC.replace("Policy Number: CAP-7781-22", "")
    matches = rank_matches(session, extract_candidates(text))
    assert matches
    assert all(m.score < 1.0 for m in matches)


def test_both_similarly_named_clients_are_offered(session):
    """She has to be able to see the near-miss, which is the whole point of
    showing candidates instead of an answer."""
    acme, other, _ = _seed(session)
    text = DEC.replace("Policy Number: CAP-7781-22", "")
    ids = {m.client_id for m in rank_matches(session, extract_candidates(text))}
    assert {acme.id, other.id} <= ids


def test_matches_are_ranked_and_limited(session):
    _seed(session)
    matches = rank_matches(session, extract_candidates(DEC), limit=1)
    assert len(matches) == 1


def test_nothing_recognisable_offers_nothing(session):
    _seed(session)
    assert rank_matches(session, extract_candidates("Dear customer,")) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.resolve'`

- [ ] **Step 3: Implement candidate extraction**

```python
# renewal/resolve/candidates.py
"""Identifiers pulled from page text, before any matching happens.

Label-driven and deliberately literal. A missing label yields nothing rather
than a guess: an invented named insured would feed a confident wrong match, and
a wrong match files a cancellation notice under the wrong client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INSURED = re.compile(
    r"(?:named\s+insured|insured\s+name|insured)\s*[:\-]\s*(.+)", re.IGNORECASE
)
_POLICY_NUMBER = re.compile(
    r"policy\s*(?:number|no\.?|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{4,24})",
    re.IGNORECASE,
)
_CITY_STATE_ZIP = re.compile(r".+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$")


@dataclass(frozen=True)
class Candidates:
    named_insured: str | None = None
    policy_numbers: list[str] = field(default_factory=list)
    address_lines: list[str] = field(default_factory=list)


def extract_candidates(page_text: str) -> Candidates:
    lines = [line.strip() for line in page_text.splitlines()]

    insured = None
    for line in lines:
        found = _INSURED.search(line)
        if found and found.group(1).strip():
            insured = found.group(1).strip()
            break

    numbers = [n.upper() for n in _POLICY_NUMBER.findall(page_text)]
    addresses = [line for line in lines if _CITY_STATE_ZIP.match(line)]
    return Candidates(
        named_insured=insured,
        policy_numbers=list(dict.fromkeys(numbers)),
        address_lines=addresses,
    )
```

- [ ] **Step 4: Implement matching**

```python
# renewal/resolve/matching.py
"""Ranked candidates with scores. Never a single silent answer.

Only an exact policy-number match reaches 1.0, which is the auto-link
threshold. Name similarity is offered and scored but is capped below it on
purpose: name-similarity auto-linking is exactly where misfiling happens, and a
cancellation notice filed under the wrong client is the worst outcome this
system can produce.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.models import Client, Policy
from renewal.resolve.candidates import Candidates

NAME_SIMILARITY_FLOOR = 0.3
NAME_SCORE_CAP = 0.95


@dataclass(frozen=True)
class Match:
    client_id: int
    policy_id: int | None
    score: float
    reason: str


def _normalize(value: str) -> str:
    return " ".join(value.split()).upper()


def rank_matches(
    session: Session, candidates: Candidates, *, limit: int = 5
) -> list[Match]:
    matches: dict[tuple[int, int | None], Match] = {}

    for number in candidates.policy_numbers:
        rows = session.execute(
            select(Policy.id, Policy.client_id).where(
                func.upper(func.trim(Policy.policy_number)) == _normalize(number)
            )
        ).all()
        for policy_id, client_id in rows:
            matches[(client_id, policy_id)] = Match(
                client_id=client_id, policy_id=policy_id, score=1.0,
                reason="exact policy number",
            )

    if candidates.named_insured:
        similarity = func.similarity(Client.display_name, candidates.named_insured)
        rows = session.execute(
            select(Client.id, similarity)
            .where(similarity > NAME_SIMILARITY_FLOOR)
            .order_by(similarity.desc())
            .limit(limit)
        ).all()
        for client_id, score in rows:
            key = (client_id, None)
            if key in matches:
                continue
            matches[key] = Match(
                client_id=client_id, policy_id=None,
                score=min(float(score), NAME_SCORE_CAP),
                reason="named insured similarity",
            )

    return sorted(matches.values(), key=lambda m: -m.score)[:limit]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_resolve.py -v`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add renewal/resolve/ tests/test_resolve.py
git commit -m "feat(resolve): candidate identifiers and ranked client matches"
```

---

## Task 18: Auto-link or queue

**Files:**
- Create: `renewal/resolve/service.py`
- Modify: `renewal/pipeline.py`
- Test: `tests/test_resolve_service.py` (create)

**Interfaces:**
- Consumes: `rank_matches`, `extract_candidates`, `page_text`, model `DocumentLink`.
- Produces: `AUTO_LINK_THRESHOLD = 1.0`, `resolve_document(session, document) -> DocumentLink | None`, `latest_link(session, document_id) -> DocumentLink | None`, `assign(session, document_id, *, client_id, policy_id, candidates) -> DocumentLink`, `unmatched(session, *, limit=50) -> list[Document]`. `run_resolve_stage(session, document)` in `pipeline`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve_service.py
from renewal.models import Client, DocumentLink, Policy
from renewal.pipeline import ingest_document
from renewal.resolve.service import (
    assign, latest_link, resolve_document, unmatched,
)
from tests.pdfmaker import make_text_pdf

DEC_LINES = [
    "DECLARATIONS PAGE",
    "Named Insured: Acme Landscaping LLC",
    "Policy Number: CAP-7781-22",
    "Effective Date: 07/01/2025",
]


def _document(session, store, lines=DEC_LINES):
    return ingest_document(session, store, data=make_text_pdf([lines]),
                           original_filename="dec.pdf", source="bulk_import",
                           agency_id=1)


def _client_with_policy(session):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name="Travelers",
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    return client, policy


def test_an_exact_policy_number_auto_links(session, store):
    client, policy = _client_with_policy(session)
    document = _document(session, store)
    link = resolve_document(session, document)
    assert link is not None
    assert (link.client_id, link.policy_id, link.method) == (
        client.id, policy.id, "auto")


def test_a_name_only_match_does_not_auto_link(session, store):
    """It queues instead, with the candidates recorded for her to choose from."""
    _client_with_policy(session)
    document = _document(session, store, [
        "Named Insured: Acme Landscaping LLC", "Renewal offer enclosed."])
    assert resolve_document(session, document) is None
    assert document.id in {d.id for d in unmatched(session)}


def test_two_exact_matches_do_not_auto_link(session, store):
    """Ambiguity is queued, never resolved silently."""
    client_a, _ = _client_with_policy(session)
    client_b = Client(display_name="Acme Landscaping Inc")
    session.add(client_b)
    session.flush()
    session.add(Policy(client_id=client_b.id, carrier_name="Travelers",
                       policy_number="CAP-7781-22",
                       line_of_business="commercial_auto"))
    session.flush()
    document = _document(session, store)
    assert resolve_document(session, document) is None


def test_manual_assignment_supersedes_and_records_what_was_offered(session, store):
    client, policy = _client_with_policy(session)
    wrong = Client(display_name="Wrong Client LLC")
    session.add(wrong)
    session.flush()
    document = _document(session, store)
    resolve_document(session, document)

    assign(session, document.id, client_id=wrong.id, policy_id=None,
           candidates=[{"client_id": client.id, "policy_id": policy.id,
                        "score": 1.0, "reason": "exact policy number"}])

    rows = session.query(DocumentLink).filter_by(
        document_id=document.id).order_by(DocumentLink.id).all()
    assert [r.method for r in rows] == ["auto", "manual"]
    assert latest_link(session, document.id).client_id == wrong.id
    assert rows[-1].candidates[0]["reason"] == "exact policy number"


def test_a_linked_document_is_not_in_the_queue(session, store):
    _client_with_policy(session)
    document = _document(session, store)
    resolve_document(session, document)
    assert document.id not in {d.id for d in unmatched(session)}


def test_resolution_is_not_repeated_for_an_already_linked_document(session, store):
    _client_with_policy(session)
    document = _document(session, store)
    resolve_document(session, document)
    resolve_document(session, document)
    assert session.query(DocumentLink).filter_by(document_id=document.id).count() == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_resolve_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.resolve.service'`

- [ ] **Step 3: Implement**

```python
# renewal/resolve/service.py
"""Attaching a document to a client.

Auto-linking requires exactly one candidate at the threshold, which only an
exact policy-number match reaches. Everything else — no match, a name-only
match, or two equally exact matches — is left unlinked and appears in the
queue. A document with no link row is unmatched; there is no status column to
disagree with reality.

Her manual assignment inserts a new link carrying the ranked list that was
shown. That row, beside the auto row it supersedes, is the Correction
equivalent: these were offered, this was right.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.models import Document, DocumentLink
from renewal.resolve.candidates import extract_candidates
from renewal.resolve.matching import Match, rank_matches
from renewal.text.store import page_text

logger = logging.getLogger(__name__)

AUTO_LINK_THRESHOLD = 1.0


def latest_link(session: Session, document_id: int) -> DocumentLink | None:
    return session.scalar(
        select(DocumentLink)
        .where(DocumentLink.document_id == document_id)
        .order_by(DocumentLink.id.desc())
        .limit(1)
    )


def unmatched(session: Session, *, limit: int = 50) -> list[Document]:
    linked = select(DocumentLink.document_id).distinct()
    return list(
        session.scalars(
            select(Document)
            .where(Document.id.not_in(linked))
            .order_by(Document.uploaded_at.desc())
            .limit(limit)
        )
    )


def candidates_for(session: Session, document: Document) -> list[Match]:
    return rank_matches(session, extract_candidates(page_text(session, document.id, 1)))


def resolve_document(session: Session, document: Document) -> DocumentLink | None:
    if latest_link(session, document.id) is not None:
        return None
    matches = candidates_for(session, document)
    exact = [m for m in matches if m.score >= AUTO_LINK_THRESHOLD]
    if len(exact) != 1:
        # Ambiguous or absent. Queued for a human, never guessed.
        logger.info(
            "document unmatched document_id=%s candidates=%s",
            document.id, len(matches),
        )
        return None
    match = exact[0]
    link = DocumentLink(
        document_id=document.id, client_id=match.client_id,
        policy_id=match.policy_id, method="auto", confidence=match.score,
        candidates=[asdict(m) for m in matches],
    )
    session.add(link)
    session.flush()
    logger.info(
        "document auto-linked document_id=%s client_id=%s policy_id=%s",
        document.id, match.client_id, match.policy_id,
    )
    return link


def assign(
    session: Session, document_id: int, *, client_id: int,
    policy_id: int | None, candidates: list[dict],
) -> DocumentLink:
    link = DocumentLink(
        document_id=document_id, client_id=client_id, policy_id=policy_id,
        method="manual", confidence=1.0, candidates=candidates,
    )
    session.add(link)
    session.flush()
    return link
```

- [ ] **Step 4: Add the stage to the pipeline**

In `renewal/pipeline.py`, add:

```python
from renewal.resolve.service import resolve_document


def run_resolve_stage(session: Session, document: Document) -> None:
    try:
        resolve_document(session, document)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("resolve stage failed document_id=%s", document.id)
```

and call `run_resolve_stage(session, document)` after `run_text_stage` in
`ingest_document`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_resolve_service.py tests/test_pipeline.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add renewal/resolve/service.py renewal/pipeline.py tests/test_resolve_service.py
git commit -m "feat(resolve): auto-link on exact policy number, queue everything else"
```

---

## Task 19: Split web.py into a router package

Behavior-free. Nothing changes except where the code lives. This lands before the calendar because the calendar is the first step that adds a substantial block of routes, and splitting after it would mean moving the code twice.

**Files:**
- Create: `renewal/web/__init__.py`, `renewal/web/runs.py`, `renewal/web/review.py`, `renewal/web/comparison.py`
- Delete: `renewal/web.py`
- Modify: `renewal/app.py`, `pyproject.toml`
- Test: `tests/test_web_review.py`, `tests/test_web_comparison.py` (unchanged — they are the proof)

**Interfaces:**
- Produces: `create_app(*, settings, store, model_client, session_factory)` from `renewal.web`, with the same signature it has today. `register(router, deps)` per module.

- [ ] **Step 1: Run the existing web tests and record the result**

Run: `pytest tests/test_web_review.py tests/test_web_comparison.py -v`
Expected: all pass. This is the baseline; the split is correct only if this exact set still passes unchanged afterwards.

- [ ] **Step 2: Create the package and move the code**

Create `renewal/web/__init__.py` holding `create_app`, the Jinja environment, the `_wbr` filter, the `html_error` handler, and the `index` route. Move the run-creation routes (`new_run`, `add_client`, `add_policy`, `create_run`) into `runs.py`; the review routes (`_latest_extraction`, `review`, `correct_field`, `reject_field`, `add_missing_field`, `promote_run`) into `review.py`; and the comparison routes (`show_comparison`, `edit_draft`, `reclassify_difference`) into `comparison.py`.

Each module exposes:

```python
from fastapi import APIRouter

router = APIRouter()


def register(app, deps) -> None:
    """deps carries settings, store, model_client, session_factory and the
    template environment. Passing it explicitly keeps module import order
    irrelevant and avoids a global."""
    app.include_router(router)
```

Delete `renewal/web.py`. Update the import in `renewal/app.py` from
`renewal.web` — the module path is unchanged, so this may need no edit; verify.

- [ ] **Step 3: Fix the package-data glob**

In `pyproject.toml`, change:

```toml
[tool.setuptools.package-data]
renewal = ["templates/*.html", "static/*"]
```

The current glob is `static/*.js` while the only static file is `app.css`, so an installed wheel ships no stylesheet. The calendar adds `app.js`, and `static/*` covers both.

Add `"renewal.text", "renewal.resolve", "renewal.dates", "renewal.calendarview", "renewal.web"` to the `packages` list in `[tool.setuptools]`.

- [ ] **Step 4: Run the tests to verify nothing changed**

Run: `pytest tests/ -v`
Expected: exactly the same set of tests passes as in Step 1. Any behavior difference means the move was not clean.

- [ ] **Step 5: Verify the app still starts**

Run: `uvicorn renewal.app:app --port 8001` and load `http://127.0.0.1:8001/`
Expected: the index renders with its stylesheet applied.

- [ ] **Step 6: Commit**

```bash
git add renewal/web/ renewal/app.py pyproject.toml
git rm renewal/web.py
git commit -m "refactor(web): split routes into a router package, no behavior change"
```

---

## Task 20: The unmatched queue

One click to assign. Creating a new client inline, because the alternative is leaving the document unfiled.

**Files:**
- Create: `renewal/web/unmatched.py`, `renewal/templates/unmatched.html`
- Modify: `renewal/web/__init__.py`
- Test: `tests/test_web_unmatched.py` (create)

**Interfaces:**
- Consumes: `unmatched`, `candidates_for`, `assign`, `latest_link`.
- Produces: routes `GET /unmatched`, `POST /unmatched/{document_id}/assign`, `POST /unmatched/{document_id}/new-client`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_unmatched.py
from renewal.models import Client, DocumentLink


def test_the_queue_lists_unmatched_documents_with_candidates(client_app, seeded):
    response = client_app.get("/unmatched")
    assert response.status_code == 200
    assert "Acme Landscaping LLC" in response.text


def test_assigning_writes_a_manual_link_with_the_candidates_shown(
    client_app, seeded, db
):
    document_id, client_id = seeded
    response = client_app.post(
        f"/unmatched/{document_id}/assign",
        data={"client_id": str(client_id)}, follow_redirects=False,
    )
    assert response.status_code == 303
    link = db.query(DocumentLink).filter_by(document_id=document_id).one()
    assert link.method == "manual"
    assert link.client_id == client_id
    assert link.candidates != []


def test_creating_a_client_from_the_queue_links_the_document(client_app, seeded, db):
    document_id, _ = seeded
    client_app.post(f"/unmatched/{document_id}/new-client",
                    data={"display_name": "Brand New LLC"},
                    follow_redirects=False)
    created = db.query(Client).filter_by(display_name="Brand New LLC").one()
    link = db.query(DocumentLink).filter_by(document_id=document_id).one()
    assert link.client_id == created.id
    assert link.candidates == []


def test_an_assigned_document_leaves_the_queue(client_app, seeded):
    document_id, client_id = seeded
    client_app.post(f"/unmatched/{document_id}/assign",
                    data={"client_id": str(client_id)}, follow_redirects=False)
    assert f"/unmatched/{document_id}/assign" not in client_app.get("/unmatched").text


def test_assigning_a_client_that_does_not_exist_is_rejected(client_app, seeded):
    document_id, _ = seeded
    response = client_app.post(f"/unmatched/{document_id}/assign",
                               data={"client_id": "999999"},
                               follow_redirects=False)
    assert response.status_code == 404
```

Add fixtures to this file following the pattern already in
`tests/test_web_review.py`: `client_app` builds the app with a temp
`BlobStore` and a stub model client; `db` yields a committing session; `seeded`
ingests one dec-page PDF whose named insured matches an existing client but
whose policy number matches nothing, and returns `(document_id, client_id)`.
Use the `clean_db` fixture, since these tests commit.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_unmatched.py -v`
Expected: FAIL with 404 on `/unmatched`

- [ ] **Step 3: Implement the routes**

```python
# renewal/web/unmatched.py
"""The queue of documents that could not be attached to a client safely.

Every assignment here is a human decision that gets recorded with the
alternatives it was chosen over. That record is the training data for making
matching better, and it is the reason assignment writes a row rather than
setting a field.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from renewal.models import Client
from renewal.resolve.service import assign, candidates_for, unmatched

router = APIRouter()
```

Route bodies:

- `GET /unmatched` — for each document from `unmatched(session)`, call
  `candidates_for` and render the document's filename, upload date, first-page
  snippet, and its ranked candidates with scores and reasons. Template
  `unmatched.html`.
- `POST /unmatched/{document_id}/assign` — take `client_id` and optional
  `policy_id` from the form, 404 if the client does not exist, recompute
  `candidates_for` and pass `[asdict(m) for m in matches]` into `assign`, then
  redirect 303 back to `/unmatched`.
- `POST /unmatched/{document_id}/new-client` — create the `Client`, then
  `assign` with `candidates=[]`, which records that nothing was offered.

- [ ] **Step 4: Write the template**

`renewal/templates/unmatched.html` extends `base.html`. One row per document.
Each candidate is a submit button labelled with the client name, the score as a
percentage, and the reason, so assignment is one click. The score is shown
because she should be able to see how close the near-miss was. An empty queue
renders a plain "Nothing unmatched" state, not an empty table.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_unmatched.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add renewal/web/unmatched.py renewal/templates/unmatched.html tests/test_web_unmatched.py
git commit -m "feat(web): unmatched document queue with one-click assignment"
```

---

## Task 21: The regex date pass

Free, exhaustive, every page. Its provenance is perfect by construction — the matched string *is* the source text — so validation can never reject it. This is the recall floor everything else builds on.

**Files:**
- Create: `renewal/dates/__init__.py`, `renewal/dates/regex_pass.py`
- Test: `tests/test_dates_regex.py` (create)

**Interfaces:**
- Consumes: `renewal.pdftext.PageText`.
- Produces: `REGEX_VERSION = "dates-regex-v1"`, `DateCandidate(date_value: date, date_type: str, source_page: int, source_text: str, confidence: float)`, `find_dates(pages: list[PageText]) -> list[DateCandidate]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dates_regex.py
from datetime import date

from renewal.dates.regex_pass import REGEX_VERSION, find_dates
from renewal.pdftext import PageText


def _pages(*texts):
    return [PageText(i + 1, t) for i, t in enumerate(texts)]


def test_slash_dates_are_read_month_first():
    """US insurance documents. 07/01/2026 is July 1st, not January 7th."""
    got = find_dates(_pages("Expiration Date: 07/01/2026"))
    assert got[0].date_value == date(2026, 7, 1)


def test_two_digit_years_resolve_to_this_century():
    got = find_dates(_pages("Effective 07/01/26"))
    assert got[0].date_value == date(2026, 7, 1)


def test_iso_dates_are_read():
    got = find_dates(_pages("Effective Date: 2026-07-01"))
    assert got[0].date_value == date(2026, 7, 1)


def test_long_form_dates_are_read():
    assert find_dates(_pages("Dated: June 1, 2026"))[0].date_value == date(2026, 6, 1)
    assert find_dates(_pages("Dated 1 June 2026"))[0].date_value == date(2026, 6, 1)
    assert find_dates(_pages("Dated Jun 1 2026"))[0].date_value == date(2026, 6, 1)


def test_source_text_is_the_matched_literal_so_validation_is_a_tautology():
    page = "Expiration Date: 07/01/2026"
    got = find_dates(_pages(page))[0]
    assert got.source_text in page


def test_page_numbers_are_carried_through():
    got = find_dates(_pages("nothing here", "Expiration Date: 07/01/2026"))
    assert got[0].source_page == 2


def test_a_nearby_label_assigns_the_type():
    got = find_dates(_pages("Cancellation Effective: 07/01/2026"))
    assert got[0].date_type == "cancellation_effective"


def test_no_label_falls_back_to_other_rather_than_guessing():
    got = find_dates(_pages("Printed 07/01/2026"))
    assert got[0].date_type == "other"
    assert got[0].confidence == 0.3


def test_a_labelled_date_carries_the_higher_heuristic_confidence():
    got = find_dates(_pages("Expiration Date: 07/01/2026"))
    assert got[0].confidence == 0.5


def test_an_impossible_date_is_skipped_not_stored():
    assert find_dates(_pages("Reference 13/45/2026")) == []


def test_over_extraction_is_the_intent():
    """Every date-shaped literal, including ones that turn out to be noise.
    A spurious date she dismisses costs two seconds."""
    got = find_dates(_pages(
        "Effective 07/01/2025 Expiration 07/01/2026 Printed 06/15/2025"))
    assert len(got) == 3


def test_the_same_literal_twice_on_a_page_yields_two_candidates():
    """Deduplication is the service's job, not this pass's; a date printed in
    two places is two pieces of evidence."""
    got = find_dates(_pages("Effective 07/01/2026 ... Effective 07/01/2026"))
    assert len(got) == 2


def test_version_is_stable():
    assert REGEX_VERSION == "dates-regex-v1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dates_regex.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.dates'`

- [ ] **Step 3: Implement**

```python
# renewal/dates/regex_pass.py
"""Every date-shaped literal on every page, for free.

The matched string is the source text, so this pass's provenance is correct by
construction and the validation step can never reject it. That makes it the
recall floor: whatever the model misses, these are already on the calendar.

The confidence numbers here are heuristics for ordering a review queue, not
probabilities. They are never presented to the user as a likelihood.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from renewal.pdftext import PageText

REGEX_VERSION = "dates-regex-v1"

LABELLED_CONFIDENCE = 0.5
UNLABELLED_CONFIDENCE = 0.3
LABEL_WINDOW = 80

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_PATTERNS = (
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"),
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})\b"),
    re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]{2,8})\.?\s+(\d{4})\b"),
)

# Ordered: the first phrase found in the window wins, so the more specific
# labels come before the ones that are substrings of them.
_LABELS = (
    ("cancellation effective", "cancellation_effective"),
    ("date of cancellation", "cancellation_effective"),
    ("non-renewal effective", "non_renewal_effective"),
    ("nonrenewal effective", "non_renewal_effective"),
    ("expiration", "policy_expiration"),
    ("expires", "policy_expiration"),
    ("renewal due", "renewal_due"),
    ("effective", "policy_effective"),
    ("payment due", "payment_due"),
    ("amount due by", "payment_due"),
    ("inspection", "inspection_deadline"),
    ("remediation", "remediation_deadline"),
    ("audit", "audit_date"),
)


@dataclass(frozen=True)
class DateCandidate:
    date_value: date
    date_type: str
    source_page: int
    source_text: str
    confidence: float


def _from_match(pattern_index: int, groups: tuple[str, ...]) -> date | None:
    try:
        if pattern_index == 0:
            month, day, year = int(groups[0]), int(groups[1]), int(groups[2])
            year += 2000 if year < 100 else 0
        elif pattern_index == 1:
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
        elif pattern_index == 2:
            month = _MONTHS.get(groups[0][:3].lower(), 0)
            day, year = int(groups[1]), int(groups[2])
        else:
            day = int(groups[0])
            month = _MONTHS.get(groups[1][:3].lower(), 0)
            year = int(groups[2])
        return date(year, month, day)
    except ValueError:
        # A date-shaped string that is not a date. Skipped rather than stored:
        # over-extraction is wanted, but an unparseable value is not a date.
        return None


def _type_for(text: str, start: int, end: int) -> str:
    window = text[max(0, start - LABEL_WINDOW) : end].lower()
    for phrase, date_type in _LABELS:
        if phrase in window:
            return date_type
    return "other"


def find_dates(pages: list[PageText]) -> list[DateCandidate]:
    out: list[DateCandidate] = []
    for page in pages:
        for index, pattern in enumerate(_PATTERNS):
            for match in pattern.finditer(page.text):
                value = _from_match(index, match.groups())
                if value is None:
                    continue
                date_type = _type_for(page.text, match.start(), match.end())
                out.append(
                    DateCandidate(
                        date_value=value,
                        date_type=date_type,
                        source_page=page.page_number,
                        source_text=match.group(0),
                        confidence=(
                            UNLABELLED_CONFIDENCE if date_type == "other"
                            else LABELLED_CONFIDENCE
                        ),
                    )
                )
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_dates_regex.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/dates/ tests/test_dates_regex.py
git commit -m "feat(dates): free exhaustive regex pass over every page"
```

---

## Task 22: The LLM date pass

Finds what the regex cannot see: prose deadlines with no digits in them. On a cancellation notice that is the single most important date in the document.

**Files:**
- Create: `renewal/dates/prompt_v1.py`, `renewal/dates/llm_pass.py`
- Modify: `renewal/config.py`, `.env.example`
- Test: `tests/test_dates_llm.py` (create)

**Interfaces:**
- Consumes: `ModelClient`, `Settings`, `PageText`, `DateCandidate`.
- Produces: `LLM_VERSION = "dates-llm-v1"`, `LlmDate` pydantic model with fields `date_value: date`, `date_type: str`, `source_page: int`, `source_text: str`, `confidence: float`, `is_derived: bool = False`, `anchor_date: date | None = None`, `anchor_source_text: str | None = None`; `parse_dates(raw: str) -> list[LlmDate]`; `find_dates_llm(pages, candidates, *, client, settings) -> list[LlmDate]`. `Settings.date_model`, `Settings.date_pages`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dates_llm.py
import json
from datetime import date

import pytest

from renewal.config import Settings
from renewal.dates.llm_pass import LLM_VERSION, find_dates_llm, parse_dates
from renewal.pdftext import PageText


class StubClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, *, model, system, content):
        self.calls.append((model, system, content))
        return self.response


def _settings(**kwargs):
    base = dict(
        database_url="", blob_root="blobs", anthropic_api_key="",
        extraction_model="m", draft_model="m", confidence_threshold=0.8,
        materiality_config="config/materiality.yaml", date_model="date-model",
        date_pages=3,
    )
    base.update(kwargs)
    return Settings(**base)


def test_parses_a_plain_date():
    raw = json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "policy_expiration",
        "source_page": 1, "source_text": "Expiration Date: 07/01/2026",
        "confidence": 0.9}]})
    got = parse_dates(raw)
    assert got[0].date_value == date(2026, 7, 1)
    assert got[0].is_derived is False


def test_parses_a_derived_date_with_its_anchor():
    raw = json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "cancellation_effective",
        "source_page": 1,
        "source_text": "within 30 days of the date of this notice",
        "confidence": 0.7, "is_derived": True,
        "anchor_date": "2026-06-01", "anchor_source_text": "Dated: June 1, 2026"}]})
    got = parse_dates(raw)[0]
    assert got.is_derived is True
    assert got.anchor_date == date(2026, 6, 1)


def test_prose_around_the_json_is_tolerated():
    raw = 'Here you go:\n{"dates": []}\nHope that helps.'
    assert parse_dates(raw) == []


def test_unparseable_output_raises_rather_than_returning_nothing():
    """Silently returning no dates would look identical to a document that
    genuinely has none."""
    with pytest.raises(ValueError):
        parse_dates("I could not read that document.")


def test_only_the_first_n_pages_are_sent():
    stub = StubClient('{"dates": []}')
    pages = [PageText(i + 1, f"page {i + 1}") for i in range(10)]
    find_dates_llm(pages, [], client=stub, settings=_settings(date_pages=3))
    sent = stub.calls[0][2][0]["text"]
    assert "page 3" in sent
    assert "page 4" not in sent


def test_regex_candidates_are_offered_to_the_model_for_typing():
    from renewal.dates.regex_pass import DateCandidate
    stub = StubClient('{"dates": []}')
    candidate = DateCandidate(date(2026, 7, 1), "other", 1, "07/01/2026", 0.3)
    find_dates_llm([PageText(1, "07/01/2026")], [candidate],
                   client=stub, settings=_settings())
    assert "07/01/2026" in stub.calls[0][2][0]["text"]


def test_the_date_model_is_used_not_the_extraction_model():
    stub = StubClient('{"dates": []}')
    find_dates_llm([PageText(1, "x")], [], client=stub, settings=_settings())
    assert stub.calls[0][0] == "date-model"


def test_version_is_stable():
    assert LLM_VERSION == "dates-llm-v1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dates_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.dates.llm_pass'`

- [ ] **Step 3: Add the settings**

In `renewal/config.py`, add to `Settings`:

```python
    date_model: str = "claude-sonnet-5"
    date_pages: int = 3
    classification_model: str = "claude-haiku-4-5-20251001"
```

and to `load_settings()`:

```python
        date_model=os.environ.get("DATE_MODEL", "claude-sonnet-5"),
        date_pages=int(os.environ.get("DATE_PAGES", "3")),
        classification_model=os.environ.get(
            "CLASSIFICATION_MODEL", "claude-haiku-4-5-20251001"
        ),
```

Add all three to `.env.example` with a comment that `DATE_PAGES` bounds cost:
deadlines live near the front of a document, and a cancellation notice is one
or two pages.

- [ ] **Step 4: Write the prompt**

```python
# renewal/dates/prompt_v1.py
"""The date-extraction prompt.

Two jobs: assign a type to the literal dates a free regex pass already found,
and find the dates that pass cannot see — deadlines stated in prose, with no
digits in them. The second job is why this call exists at all.

Over-extraction is explicitly requested. A spurious date she dismisses costs
two seconds; a missed cancellation deadline is the entire risk of this product.
"""

VERSION = "dates-llm-v1"

SYSTEM = """\
You read insurance documents and report every date in them.

Report a date even when you are unsure it matters. A date the reader dismisses
costs them two seconds; a date you omit may be a missed deadline. Prefer
reporting too many.

Every date must carry the exact text you read it from, copied character for
character from the page you cite. If you cannot copy the text exactly, do not
report the date.

Some deadlines are stated in prose rather than printed as a date — "within 30
days of the date of this notice". Report these too. Set is_derived true, put
the prose in source_text, compute date_value, and record the date you counted
from in anchor_date with its own exact text in anchor_source_text. If no anchor
date is printed on the page, still report the deadline with is_derived true and
leave the anchor fields null.

date_type must be one of: policy_effective, policy_expiration, renewal_due,
cancellation_effective, non_renewal_effective, payment_due, inspection_deadline,
remediation_deadline, audit_date, other. Use other rather than guessing.

Answer with JSON only:
{"dates": [{"date_value": "YYYY-MM-DD", "date_type": "...", "source_page": 1,
  "source_text": "...", "confidence": 0.0, "is_derived": false,
  "anchor_date": null, "anchor_source_text": null}]}
"""

USER_TEMPLATE = """\
A regex pass already found these literal dates. Assign each one a date_type,
and add any dates it missed:

{candidates}

Document:

{document_text}
"""
```

- [ ] **Step 5: Implement the pass**

```python
# renewal/dates/llm_pass.py
"""The bounded model pass over the front of a document."""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from pydantic import BaseModel, Field

from renewal.config import Settings
from renewal.dates import prompt_v1
from renewal.dates.regex_pass import DateCandidate
from renewal.pdftext import PageText
from renewal.providers import ModelClient, text_block

logger = logging.getLogger(__name__)

LLM_VERSION = prompt_v1.VERSION


class LlmDate(BaseModel):
    date_value: date
    date_type: str
    source_page: int = Field(ge=1)
    source_text: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_derived: bool = False
    anchor_date: date | None = None
    anchor_source_text: str | None = None


class _Payload(BaseModel):
    dates: list[LlmDate]


def parse_dates(raw: str) -> list[LlmDate]:
    """Raises when no JSON object can be recovered. Returning an empty list on
    unparseable output would be indistinguishable from a document that
    genuinely has no dates, and the caller needs to tell those apart."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return _Payload.model_validate(json.loads(match.group(0))).dates


def find_dates_llm(
    pages: list[PageText],
    candidates: list[DateCandidate],
    *,
    client: ModelClient,
    settings: Settings,
) -> list[LlmDate]:
    head = pages[: settings.date_pages]
    document_text = "\n".join(
        f"=== PAGE {p.page_number} ===\n{p.text}" for p in head
    )
    listed = "\n".join(
        f"- page {c.source_page}: {c.source_text}"
        for c in candidates
        if c.source_page <= settings.date_pages
    ) or "(none)"
    content = [
        text_block(
            prompt_v1.USER_TEMPLATE.format(
                candidates=listed, document_text=document_text
            )
        )
    ]
    raw = client.complete(
        model=settings.date_model, system=prompt_v1.SYSTEM, content=content
    )
    return parse_dates(raw)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_dates_llm.py -v`
Expected: 8 passed

- [ ] **Step 7: Commit**

```bash
git add renewal/dates/prompt_v1.py renewal/dates/llm_pass.py renewal/config.py .env.example tests/test_dates_llm.py
git commit -m "feat(dates): bounded LLM pass for typing and prose deadlines"
```

---

## Task 23: Validate, store, confirm, dismiss

If the cited text is not on that page, the date is rejected rather than stored. This deliberately diverges from field extraction, which keeps an unverifiable field at zero confidence: a field at zero confidence is evidence about the extractor, but a date that renders on a calendar is a claim.

**Files:**
- Create: `renewal/dates/service.py`
- Modify: `renewal/pipeline.py`
- Test: `tests/test_dates_service.py` (create)

**Interfaces:**
- Consumes: `find_dates`, `find_dates_llm`, `page_text`, models `DocumentDate`, `DateEvent`.
- Produces: `extract_dates(session, document, pages, *, client, settings) -> list[DocumentDate]`, `status_of(session, document_date_id) -> str`, `confirm(session, document_date_id, *, actor="human") -> DateEvent`, `dismiss(session, document_date_id, *, actor="human") -> DateEvent`, `rejected_count` on the returned stats. `run_dates_stage(session, document, ...)` in `pipeline`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dates_service.py
import json
from datetime import date

from renewal.dates.service import confirm, dismiss, extract_dates, status_of
from renewal.models import DocumentDate
from renewal.pdftext import PageText
from tests.test_dates_llm import StubClient, _settings

PAGE = "Named Insured: Acme\nExpiration Date: 07/01/2026\nDated: June 1, 2026"


def _pages():
    return [PageText(1, PAGE)]


def _document(session, store):
    from renewal.pipeline import ingest_document
    from tests.pdfmaker import make_text_pdf
    return ingest_document(session, store, data=make_text_pdf([PAGE.splitlines()]),
                           original_filename="d.pdf", source="bulk_import",
                           agency_id=1)


def test_regex_dates_are_stored_with_their_pass_recorded(session, store):
    document = _document(session, store)
    stub = StubClient('{"dates": []}')
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    rows = session.query(DocumentDate).filter_by(document_id=document.id).all()
    assert rows
    assert {r.pass_name for r in rows} == {"regex"}


def test_a_hallucinated_source_text_is_rejected_not_stored(session, store):
    """The worst failure this system can produce is a confidently wrong date."""
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-09-09", "date_type": "cancellation_effective",
        "source_page": 1, "source_text": "Cancellation Effective: 09/09/2026",
        "confidence": 0.95}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    stored = session.query(DocumentDate).filter_by(
        document_id=document.id, pass_name="llm").all()
    assert stored == []


def test_a_source_page_out_of_range_is_rejected(session, store):
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "policy_expiration",
        "source_page": 99, "source_text": "Expiration Date: 07/01/2026",
        "confidence": 0.9}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    assert session.query(DocumentDate).filter_by(
        document_id=document.id, pass_name="llm").all() == []


def test_a_verified_llm_date_is_stored(session, store):
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "policy_expiration",
        "source_page": 1, "source_text": "Expiration Date: 07/01/2026",
        "confidence": 0.9}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    row = session.query(DocumentDate).filter_by(
        document_id=document.id, pass_name="llm").one()
    assert row.date_type == "policy_expiration"


def test_a_derived_date_with_a_verified_anchor_is_stored_with_it(session, store):
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "cancellation_effective",
        "source_page": 1, "source_text": "Dated: June 1, 2026",
        "confidence": 0.7, "is_derived": True, "anchor_date": "2026-06-01",
        "anchor_source_text": "Dated: June 1, 2026"}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    row = session.query(DocumentDate).filter_by(is_derived=True).one()
    assert row.anchor_date == date(2026, 6, 1)


def test_a_derived_date_with_an_unverifiable_anchor_is_kept_and_flagged(
    session, store
):
    """Stored so it reaches the calendar, flagged so it never reads as fact."""
    document = _document(session, store)
    stub = StubClient(json.dumps({"dates": [{
        "date_value": "2026-07-01", "date_type": "cancellation_effective",
        "source_page": 1, "source_text": "Dated: June 1, 2026",
        "confidence": 0.7, "is_derived": True, "anchor_date": "2026-06-01",
        "anchor_source_text": "Dated: some other day entirely"}]}))
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    row = session.query(DocumentDate).filter_by(is_derived=True).one()
    assert row.anchor_date is None
    assert row.anchor_source_text is None


def test_an_unparseable_model_response_does_not_lose_the_regex_dates(session, store):
    document = _document(session, store)
    extract_dates(session, document, _pages(),
                  client=StubClient("I cannot read that."), settings=_settings())
    assert session.query(DocumentDate).filter_by(pass_name="regex").count() > 0


def test_a_date_is_unconfirmed_until_confirmed(session, store):
    document = _document(session, store)
    extract_dates(session, document, _pages(), client=StubClient('{"dates": []}'),
                  settings=_settings())
    row = session.query(DocumentDate).first()
    assert status_of(session, row.id) == "unconfirmed"
    confirm(session, row.id)
    assert status_of(session, row.id) == "confirmed"


def test_dismissal_after_confirmation_wins(session, store):
    """Latest event wins; she can change her mind."""
    document = _document(session, store)
    extract_dates(session, document, _pages(), client=StubClient('{"dates": []}'),
                  settings=_settings())
    row = session.query(DocumentDate).first()
    confirm(session, row.id)
    dismiss(session, row.id)
    assert status_of(session, row.id) == "dismissed"


def test_re_running_at_the_same_versions_does_not_duplicate(session, store):
    document = _document(session, store)
    stub = StubClient('{"dates": []}')
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    before = session.query(DocumentDate).count()
    extract_dates(session, document, _pages(), client=stub, settings=_settings())
    assert session.query(DocumentDate).count() == before
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dates_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.dates.service'`

- [ ] **Step 3: Implement**

```python
# renewal/dates/service.py
"""Validating and storing extracted dates, and recording her judgment on them.

A date whose cited source text is not on the page it names is rejected, not
stored at low confidence. Field extraction keeps unverifiable fields because an
unverifiable field is evidence about the extractor; a date is different,
because a date renders on a calendar as a claim, and a wrong
cancellation-effective date shown confidently is the worst thing this system
can do.

Both passes' rows are kept. The calendar collapses them for display; storage
keeps them apart so their recall can be measured separately.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.dates.llm_pass import LLM_VERSION, LlmDate, find_dates_llm
from renewal.dates.regex_pass import REGEX_VERSION, find_dates
from renewal.models import DateEvent, Document, DocumentDate
from renewal.pdftext import PageText
from renewal.providers import ModelClient

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _on_page(text: str, pages: list[PageText], page_number: int) -> bool:
    if not 1 <= page_number <= len(pages):
        return False
    if not _normalize(text):
        return False
    return _normalize(text) in _normalize(pages[page_number - 1].text)


def _already_extracted(session: Session, document_id: int, version: str) -> bool:
    return session.scalar(
        select(DocumentDate.id)
        .where(DocumentDate.document_id == document_id)
        .where(DocumentDate.extractor_version == version)
        .limit(1)
    ) is not None


def _store_llm_date(
    session: Session, document_id: int, item: LlmDate, pages: list[PageText]
) -> DocumentDate | None:
    if not _on_page(item.source_text, pages, item.source_page):
        logger.info(
            "date rejected document_id=%s page=%s reason=source_text_not_on_page",
            document_id, item.source_page,
        )
        return None
    anchor_ok = bool(item.anchor_source_text) and _on_page(
        item.anchor_source_text or "", pages, item.source_page
    )
    row = DocumentDate(
        document_id=document_id,
        date_value=item.date_value,
        date_type=item.date_type,
        source_page=item.source_page,
        source_text=item.source_text,
        confidence=item.confidence,
        extractor_version=LLM_VERSION,
        pass_name="llm",
        is_derived=item.is_derived,
        # An unverified anchor is dropped rather than shown: the UI would
        # otherwise display arithmetic it cannot stand behind. The date itself
        # survives and is flagged as derived with no anchor.
        anchor_date=item.anchor_date if anchor_ok else None,
        anchor_source_text=item.anchor_source_text if anchor_ok else None,
    )
    session.add(row)
    return row


def extract_dates(
    session: Session,
    document: Document,
    pages: list[PageText],
    *,
    client: ModelClient,
    settings: Settings,
) -> list[DocumentDate]:
    rows: list[DocumentDate] = []

    if not _already_extracted(session, document.id, REGEX_VERSION):
        for candidate in find_dates(pages):
            row = DocumentDate(
                document_id=document.id,
                date_value=candidate.date_value,
                date_type=candidate.date_type,
                source_page=candidate.source_page,
                source_text=candidate.source_text,
                confidence=candidate.confidence,
                extractor_version=REGEX_VERSION,
                pass_name="regex",
            )
            session.add(row)
            rows.append(row)

    if not _already_extracted(session, document.id, LLM_VERSION):
        try:
            found = find_dates_llm(
                pages, find_dates(pages), client=client, settings=settings
            )
        except Exception:  # noqa: BLE001 - the regex floor must survive this
            logger.exception("date llm pass failed document_id=%s", document.id)
            found = []
        for item in found:
            row = _store_llm_date(session, document.id, item, pages)
            if row is not None:
                rows.append(row)

    session.flush()
    logger.info(
        "dates extracted document_id=%s stored=%s", document.id, len(rows)
    )
    return rows


def status_of(session: Session, document_date_id: int) -> str:
    """Unconfirmed until she says otherwise. The latest event wins, so she can
    change her mind and the history of the change is kept."""
    action = session.scalar(
        select(DateEvent.action)
        .where(DateEvent.document_date_id == document_date_id)
        .order_by(DateEvent.id.desc())
        .limit(1)
    )
    return action or "unconfirmed"


def confirm(session: Session, document_date_id: int, *, actor: str = "human"):
    event = DateEvent(document_date_id=document_date_id, action="confirmed",
                      actor=actor)
    session.add(event)
    session.flush()
    return event


def dismiss(session: Session, document_date_id: int, *, actor: str = "human"):
    event = DateEvent(document_date_id=document_date_id, action="dismissed",
                      actor=actor)
    session.add(event)
    session.flush()
    return event
```

- [ ] **Step 4: Add the stage to the pipeline**

In `renewal/pipeline.py`, add a `run_dates_stage(session, document, *, client, settings)` that reads the stored `DocumentText` rows back into `PageText` objects and calls `extract_dates`, wrapped in the same try/except as the other stages. Extend `ingest_document` to take `model_client` and `settings` and call it after `run_resolve_stage`. Update `tests/test_pipeline.py` fixtures to pass a stub client returning `'{"dates": []}'`.

Reading text back from `DocumentText` rather than re-reading the PDF is what makes date extraction work identically for an email body, which has no PDF.

`ingest_document` gains two required keyword arguments here, so every existing
caller must be updated in this same commit:

- `scripts/bulk_import.py` — `import_tree` takes `client` and `settings` and
  passes them through; `main()` builds the client with `build_client(settings)`.
- `tests/test_pipeline.py`, `tests/test_resolve_service.py`,
  `tests/test_text_store.py` — pass a stub client returning `'{"dates": []}'`.

Run `grep -rn "ingest_document(" --include="*.py" .` and confirm every hit is
updated before committing.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_dates_service.py tests/test_pipeline.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add renewal/dates/service.py renewal/pipeline.py tests/test_dates_service.py tests/test_pipeline.py
git commit -m "feat(dates): validate against the cited page, store, confirm, dismiss"
```

---

## Task 24: The agenda query

Agenda is the default view. It is what a list on paper looks like, which is what she is replacing.

**Files:**
- Create: `renewal/calendarview/__init__.py`, `renewal/calendarview/agenda.py`
- Test: `tests/test_agenda.py` (create)

**Interfaces:**
- Consumes: models `DocumentDate`, `DateEvent`, `ManualDate`, `ManualDateEvent`, `DocumentLink`, `Client`, `DocumentClassification`.
- Produces: `AgendaEntry(kind, source_id, date_value, date_type, title, client_id, client_name, document_id, status, confidence, is_derived, anchor_date, anchor_source_text, source_text, source_page, escalated)`; `agenda(session, *, agency_id, client_id=None, date_types=None, statuses=None, start=None, end=None, limit=500) -> list[AgendaEntry]`; `ESCALATED_DATE_TYPES`, `ESCALATED_DOC_CLASSES`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agenda.py
from datetime import date

from renewal.calendarview.agenda import agenda
from renewal.dates.service import confirm, dismiss
from renewal.models import (
    Client, DocumentClassification, DocumentDate, DocumentLink, ManualDate,
)


def _document(session):
    from renewal.models import Document
    document = Document(blob_sha256="a" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    return document


def _dated(session, document, value, date_type, **kwargs):
    row = DocumentDate(document_id=document.id, date_value=value,
                       date_type=date_type, source_page=1, source_text="x",
                       confidence=0.5, extractor_version="dates-regex-v1",
                       pass_name="regex", **kwargs)
    session.add(row)
    session.flush()
    return row


def _linked(session, document, name="Acme Landscaping LLC"):
    client = Client(display_name=name)
    session.add(client)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="auto", confidence=1.0, candidates=[]))
    session.flush()
    return client


def test_an_extracted_date_appears_with_its_client(session):
    document = _document(session)
    client = _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    entries = agenda(session, agency_id=1)
    assert entries[0].client_name == client.display_name
    assert entries[0].status == "unconfirmed"


def test_a_relinked_document_moves_its_dates_with_no_backfill(session):
    """The reason document_date carries no client_id."""
    document = _document(session)
    _linked(session, document, "Wrong Client LLC")
    right = Client(display_name="Right Client LLC")
    session.add(right)
    session.flush()
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    session.add(DocumentLink(document_id=document.id, client_id=right.id,
                             method="manual", confidence=1.0, candidates=[]))
    session.flush()
    assert agenda(session, agency_id=1)[0].client_name == "Right Client LLC"


def test_status_reflects_the_latest_event(session):
    document = _document(session)
    _linked(session, document)
    row = _dated(session, document, date(2026, 7, 1), "policy_expiration")
    confirm(session, row.id)
    assert agenda(session, agency_id=1)[0].status == "confirmed"
    dismiss(session, row.id)
    assert agenda(session, agency_id=1)[0].status == "dismissed"


def test_dismissed_dates_are_excluded_by_default(session):
    document = _document(session)
    _linked(session, document)
    row = _dated(session, document, date(2026, 7, 1), "policy_expiration")
    dismiss(session, row.id)
    assert agenda(session, agency_id=1, statuses=("unconfirmed", "confirmed")) == []


def test_manual_dates_appear_alongside_extracted_ones(session):
    document = _document(session)
    client = _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    session.add(ManualDate(agency_id=1, client_id=client.id,
                           title="Call about the audit",
                           date_value=date(2026, 6, 1), date_type="audit_date",
                           created_by="human"))
    session.flush()
    kinds = [e.kind for e in agenda(session, agency_id=1)]
    assert sorted(kinds) == ["document_date", "manual_date"]


def test_a_cancellation_date_is_escalated_and_pinned_first(session):
    """Regardless of date proximity. This is the interrupt-everything event."""
    document = _document(session)
    _linked(session, document)
    _dated(session, document, date(2026, 1, 1), "policy_expiration")
    _dated(session, document, date(2027, 12, 1), "cancellation_effective")
    entries = agenda(session, agency_id=1)
    assert entries[0].date_type == "cancellation_effective"
    assert entries[0].escalated is True


def test_a_misclassified_cancellation_notice_still_escalates(session):
    """Display must never depend on classification being right, so the
    date_type escalates on its own."""
    document = _document(session)
    _linked(session, document)
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="unknown", confidence=0.1,
                                       classifier_version="classify-v1",
                                       model_id="stub"))
    session.flush()
    _dated(session, document, date(2027, 12, 1), "cancellation_effective")
    assert agenda(session, agency_id=1)[0].escalated is True


def test_a_cancellation_notice_escalates_its_other_dates_too(session):
    document = _document(session)
    _linked(session, document)
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="cancellation_notice",
                                       confidence=0.9,
                                       classifier_version="classify-v1",
                                       model_id="stub"))
    session.flush()
    _dated(session, document, date(2027, 12, 1), "payment_due")
    assert agenda(session, agency_id=1)[0].escalated is True


def test_non_escalated_entries_are_ordered_by_date(session):
    document = _document(session)
    _linked(session, document)
    _dated(session, document, date(2026, 9, 1), "policy_expiration")
    _dated(session, document, date(2026, 7, 1), "renewal_due")
    values = [e.date_value for e in agenda(session, agency_id=1)]
    assert values == sorted(values)


def test_filters_by_client_and_type_and_range(session):
    document = _document(session)
    client = _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    _dated(session, document, date(2026, 9, 1), "audit_date")
    assert len(agenda(session, agency_id=1, client_id=client.id)) == 2
    assert len(agenda(session, agency_id=1, date_types=("audit_date",))) == 1
    assert len(agenda(session, agency_id=1, start=date(2026, 8, 1))) == 1


def test_derived_dates_carry_their_arithmetic_into_the_entry(session):
    document = _document(session)
    _linked(session, document)
    _dated(session, document, date(2026, 7, 1), "cancellation_effective",
           is_derived=True, anchor_date=date(2026, 6, 1),
           anchor_source_text="Dated: June 1, 2026")
    entry = agenda(session, agency_id=1)[0]
    assert entry.is_derived is True
    assert entry.anchor_date == date(2026, 6, 1)


def test_an_unlinked_documents_dates_still_appear(session):
    """A date is useful even when we do not yet know whose it is."""
    document = _document(session)
    _dated(session, document, date(2026, 7, 1), "policy_expiration")
    entry = agenda(session, agency_id=1)[0]
    assert entry.client_id is None
    assert entry.client_name is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_agenda.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.calendarview'`

- [ ] **Step 3: Implement**

```python
# renewal/calendarview/agenda.py
"""The agenda: every date she needs to see, in one list.

Escalation is deliberately doubled up. An entry is pinned when the document was
classified as a cancellation or non-renewal notice, OR when the date's own type
says so. The second condition means a misclassified notice still escalates —
display never depends on classification being right, which is the whole reason
classification is not allowed to gate anything.

Dates carry no client_id. The client comes from the document's latest link, so
re-filing a misfiled document moves every date on it at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.models import (
    Client, DateEvent, DocumentClassification, DocumentDate, DocumentLink,
    ManualDate, ManualDateEvent,
)

ESCALATED_DATE_TYPES = ("cancellation_effective", "non_renewal_effective")
ESCALATED_DOC_CLASSES = ("cancellation_notice", "non_renewal_notice")
DEFAULT_STATUSES = ("unconfirmed", "confirmed")


@dataclass(frozen=True)
class AgendaEntry:
    kind: str
    source_id: int
    date_value: date
    date_type: str
    title: str
    client_id: int | None
    client_name: str | None
    document_id: int | None
    status: str
    confidence: float | None
    is_derived: bool
    anchor_date: date | None
    anchor_source_text: str | None
    source_text: str | None
    source_page: int | None
    escalated: bool


def _latest_link_subquery():
    """One row per document: its most recent link."""
    ranked = select(
        DocumentLink.document_id.label("document_id"),
        DocumentLink.client_id.label("client_id"),
        func.row_number()
        .over(partition_by=DocumentLink.document_id,
              order_by=DocumentLink.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.document_id, ranked.c.client_id).where(
        ranked.c.rn == 1
    ).subquery()


def _latest_class_subquery():
    ranked = select(
        DocumentClassification.document_id.label("document_id"),
        DocumentClassification.doc_class.label("doc_class"),
        func.row_number()
        .over(partition_by=DocumentClassification.document_id,
              order_by=DocumentClassification.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.document_id, ranked.c.doc_class).where(
        ranked.c.rn == 1
    ).subquery()


def _latest_event_subquery(model, fk_name: str):
    column = getattr(model, fk_name)
    ranked = select(
        column.label("parent_id"),
        model.action.label("action"),
        func.row_number()
        .over(partition_by=column, order_by=model.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.parent_id, ranked.c.action).where(
        ranked.c.rn == 1
    ).subquery()


def agenda(
    session: Session,
    *,
    agency_id: int,
    client_id: int | None = None,
    date_types: tuple[str, ...] | None = None,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> list[AgendaEntry]:
    links = _latest_link_subquery()
    classes = _latest_class_subquery()
    events = _latest_event_subquery(DateEvent, "document_date_id")

    query = (
        select(DocumentDate, links.c.client_id, Client.display_name,
               classes.c.doc_class, events.c.action)
        .outerjoin(links, links.c.document_id == DocumentDate.document_id)
        .outerjoin(Client, Client.id == links.c.client_id)
        .outerjoin(classes, classes.c.document_id == DocumentDate.document_id)
        .outerjoin(events, events.c.parent_id == DocumentDate.id)
    )
    if client_id is not None:
        query = query.where(links.c.client_id == client_id)
    if date_types:
        query = query.where(DocumentDate.date_type.in_(date_types))
    if start is not None:
        query = query.where(DocumentDate.date_value >= start)
    if end is not None:
        query = query.where(DocumentDate.date_value <= end)

    entries: list[AgendaEntry] = []
    for row, linked_client, client_name, doc_class, action in session.execute(query):
        status = action or "unconfirmed"
        if status not in statuses:
            continue
        entries.append(
            AgendaEntry(
                kind="document_date",
                source_id=row.id,
                date_value=row.date_value,
                date_type=row.date_type,
                title=row.date_type.replace("_", " "),
                client_id=linked_client,
                client_name=client_name,
                document_id=row.document_id,
                status=status,
                confidence=row.confidence,
                is_derived=row.is_derived,
                anchor_date=row.anchor_date,
                anchor_source_text=row.anchor_source_text,
                source_text=row.source_text,
                source_page=row.source_page,
                escalated=(
                    row.date_type in ESCALATED_DATE_TYPES
                    or doc_class in ESCALATED_DOC_CLASSES
                ),
            )
        )

    manual_events = _latest_event_subquery(ManualDateEvent, "manual_date_id")
    manual_query = (
        select(ManualDate, Client.display_name, manual_events.c.action)
        .outerjoin(Client, Client.id == ManualDate.client_id)
        .outerjoin(manual_events, manual_events.c.parent_id == ManualDate.id)
        .where(ManualDate.agency_id == agency_id)
    )
    if client_id is not None:
        manual_query = manual_query.where(ManualDate.client_id == client_id)
    if date_types:
        manual_query = manual_query.where(ManualDate.date_type.in_(date_types))
    if start is not None:
        manual_query = manual_query.where(ManualDate.date_value >= start)
    if end is not None:
        manual_query = manual_query.where(ManualDate.date_value <= end)

    for row, client_name, action in session.execute(manual_query):
        # A date she typed in herself is confirmed by the act of typing it.
        status = action or "confirmed"
        if status not in statuses:
            continue
        entries.append(
            AgendaEntry(
                kind="manual_date",
                source_id=row.id,
                date_value=row.date_value,
                date_type=row.date_type,
                title=row.title,
                client_id=row.client_id,
                client_name=client_name,
                document_id=None,
                status=status,
                confidence=None,
                is_derived=False,
                anchor_date=None,
                anchor_source_text=None,
                source_text=None,
                source_page=None,
                escalated=row.date_type in ESCALATED_DATE_TYPES,
            )
        )

    entries.sort(key=lambda e: (not e.escalated, e.date_value))
    return entries[:limit]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_agenda.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/calendarview/ tests/test_agenda.py
git commit -m "feat(calendar): agenda query with escalation and latest-event status"
```

---

## Task 25: The calendar screen

Confirmation is one click. Dismissal is one keystroke. Unconfirmed dates never render as fact.

**Files:**
- Create: `renewal/web/calendar.py`, `renewal/templates/calendar.html`, `renewal/static/app.js`
- Modify: `renewal/web/__init__.py`, `renewal/static/app.css`
- Test: `tests/test_web_calendar.py` (create)

**Interfaces:**
- Consumes: `agenda`, `confirm`, `dismiss`, models `ManualDate`.
- Produces: routes `GET /calendar` (agenda, default), `GET /calendar/month`, `POST /dates/{id}/confirm`, `POST /dates/{id}/dismiss`, `POST /manual-dates`, `POST /manual-dates/{id}/dismiss`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_calendar.py
from datetime import date

from renewal.dates.service import status_of
from renewal.models import ManualDate


def test_agenda_is_the_default_view(client_app, seeded_date):
    response = client_app.get("/calendar")
    assert response.status_code == 200
    assert 'data-view="agenda"' in response.text


def test_an_unconfirmed_date_is_visually_marked(client_app, seeded_date):
    """Never render an unconfirmed extracted date as fact."""
    body = client_app.get("/calendar").text
    assert "unconfirmed" in body


def test_confirming_is_one_post(client_app, seeded_date, db):
    response = client_app.post(f"/dates/{seeded_date}/confirm",
                               follow_redirects=False)
    assert response.status_code == 303
    assert status_of(db, seeded_date) == "confirmed"


def test_dismissing_is_one_post(client_app, seeded_date, db):
    client_app.post(f"/dates/{seeded_date}/dismiss", follow_redirects=False)
    assert status_of(db, seeded_date) == "dismissed"


def test_a_cancellation_date_is_pinned_and_marked_escalated(
    client_app, seeded_cancellation
):
    body = client_app.get("/calendar").text
    assert "escalated" in body
    assert body.index("escalated") < body.index("policy expiration")


def test_a_derived_date_shows_its_arithmetic(client_app, seeded_derived):
    body = client_app.get("/calendar").text
    assert "computed" in body


def test_she_can_add_a_date_that_came_from_no_document(client_app, db):
    """The only way this replaces the handwritten list."""
    response = client_app.post("/manual-dates", data={
        "title": "Call about the audit", "date_value": "2026-09-01",
        "date_type": "audit_date"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.query(ManualDate).filter_by(title="Call about the audit").count() == 1


def test_filters_are_applied(client_app, seeded_date):
    assert client_app.get("/calendar?date_type=audit_date").status_code == 200


def test_month_view_is_reachable(client_app, seeded_date):
    assert client_app.get("/calendar/month").status_code == 200


def test_each_entry_links_to_its_source_document(client_app, seeded_date):
    assert "/documents/" in client_app.get("/calendar").text
```

Fixtures follow `tests/test_web_review.py`. `seeded_date` ingests one document with an extracted `policy_expiration` date and returns its `document_date.id`; `seeded_cancellation` adds a `cancellation_effective` date dated far in the future plus a nearer `policy_expiration`; `seeded_derived` adds an `is_derived` date with an anchor. Use `clean_db`, since these commit.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_calendar.py -v`
Expected: FAIL with 404 on `/calendar`

- [ ] **Step 3: Implement the routes**

`renewal/web/calendar.py`:

- `GET /calendar` — read filters (`client_id`, `date_type`, `status`) from the query string, call `agenda`, render `calendar.html` with `view="agenda"`.
- `GET /calendar/month` — same query, grouped by day, rendered as a month grid, `view="month"`.
- `POST /dates/{id}/confirm` and `/dismiss` — call `confirm`/`dismiss`, redirect 303 back to the referring calendar URL so filters survive the round trip.
- `POST /manual-dates` — create a `ManualDate` for agency 1 from `title`, `date_value`, `date_type`, optional `client_id` and `notes`; redirect 303.
- `POST /manual-dates/{id}/dismiss` — append a `ManualDateEvent`.

- [ ] **Step 4: Write the template**

`calendar.html` extends `base.html` and carries `data-view` on its container.

Each entry renders, in one dense row: the date, the client name (or "unmatched"), the date type, and a link to `/documents/{id}` for the source. Escalated entries carry an `escalated` class and sit at the top of the list, ahead of everything regardless of date proximity.

Status is visible, not inferred: an entry with `status == "unconfirmed"` carries an `unconfirmed` class and a label, and never renders in the same style as a confirmed one. A `is_derived` entry shows `computed — {{ anchor_date }} + arithmetic` when it has an anchor, and `computed — anchor not verified` when it does not.

Confirm is a one-click form button per row. The add-a-date form sits at the top of the agenda, not behind a link.

In `app.css`, add rules for `.escalated`, `.unconfirmed`, and `.derived`. Escalation must read at a glance without relying on color alone — use a weight or rule change as well, since color alone fails for a colorblind reader.

- [ ] **Step 5: Add keystroke dismissal**

`renewal/static/app.js`: focusable agenda rows; `d` on the focused row submits its dismiss form; `c` confirms; `j`/`k` move between rows. Guard on `event.target` being the document body or a row, so typing in the add-a-date form is unaffected. No dependencies, no build step.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_web_calendar.py -v`
Expected: 10 passed

- [ ] **Step 7: Verify by hand**

Run the app, load `/calendar`, and confirm: agenda is the default, unconfirmed rows are distinguishable from confirmed ones at a glance, a cancellation date sits at the top regardless of its distance, `d` dismisses the focused row, and the add-a-date form is visible without scrolling.

- [ ] **Step 8: Commit**

```bash
git add renewal/web/calendar.py renewal/templates/calendar.html renewal/static/ tests/test_web_calendar.py
git commit -m "feat(calendar): agenda and month views with one-click confirmation"
```

---

## Task 26: The .ics feed

Read-only, subscribe-only. Nothing is written into her calendar and there is no two-way sync.

⚠️ The token in this URL is a bearer credential. It will sync to her phone with her calendar configuration, and anyone holding the link can read the whole book's deadline map until it is regenerated.

**Files:**
- Create: `renewal/calendarview/ics.py`, `renewal/web/settings.py`, `renewal/templates/settings.html`
- Test: `tests/test_ics.py`, `tests/test_web_settings.py` (create)

**Interfaces:**
- Consumes: `agenda`, model `Agency`.
- Produces: `render_ics(entries: list[AgendaEntry], *, calendar_name: str) -> str`; routes `GET /calendar/{token}.ics`, `GET /settings`, `POST /settings/regenerate-ics-token`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ics.py
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
    assert "Smith\\, Jones\; & Co" in body
```

```python
# tests/test_web_settings.py
def test_the_ics_feed_needs_the_token(client_app):
    assert client_app.get("/calendar/not-the-token.ics").status_code == 404


def test_the_ics_feed_serves_with_the_token(client_app, agency_token):
    response = client_app.get(f"/calendar/{agency_token}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")


def test_regenerating_the_token_kills_the_old_link(client_app, agency_token):
    client_app.post("/settings/regenerate-ics-token", follow_redirects=False)
    assert client_app.get(f"/calendar/{agency_token}.ics").status_code == 404


def test_the_settings_page_warns_that_the_link_is_a_credential(client_app):
    body = client_app.get("/settings").text
    assert "anyone with this link" in body.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_ics.py tests/test_web_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.calendarview.ics'`

- [ ] **Step 3: Implement the renderer**

```python
# renewal/calendarview/ics.py
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

_ESCAPES = (("\\", "\\\\"), (";", "\;"), (",", "\\,"), ("\n", "\\n"))


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
            f"UID:{entry.kind}-{entry.source_id}@renewal",
            f"DTSTART;VALUE=DATE:{stamp}",
            f"SUMMARY:{_escape(summary)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
```

No `DTSTAMP`: it would change on every render and make the output unstable,
which breaks the stable-UID guarantee her calendar app relies on to update
rather than duplicate events.

- [ ] **Step 4: Implement the routes**

`renewal/web/settings.py`:

- `GET /calendar/{token}.ics` — look up the agency by `ics_token`; 404 when there is no match, with no hint about whether the token merely expired. Render `agenda(...)` with `statuses=("unconfirmed", "confirmed")` through `render_ics`, returned with `media_type="text/calendar"`.
- `GET /settings` — show the intake address, the feed URL, and a plainly worded warning that anyone with this link can read every client name and deadline until the token is regenerated.
- `POST /settings/regenerate-ics-token` — insert a new token with `secrets.token_urlsafe(32)`. This is the one field on `agency` that is updated in place; note in a comment that the old link must stop working immediately, which an append-only row would not achieve.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_ics.py tests/test_web_settings.py -v`
Expected: 10 passed

- [ ] **Step 6: Verify by subscribing**

Copy the feed URL into a calendar app and confirm the events appear, that an unconfirmed one is labelled, and that regenerating the token breaks the subscription.

- [ ] **Step 7: Commit**

```bash
git add renewal/calendarview/ics.py renewal/web/settings.py renewal/templates/settings.html tests/test_ics.py tests/test_web_settings.py
git commit -m "feat(calendar): read-only ics feed behind a revocable token"
```

---

## Task 27: Wire dates and matching into the eval run

The scorers exist from Tasks 1–3 but nothing calls them yet. Recall is the number that matters, so it needs to be on screen every run.

**Files:**
- Modify: `evals/test_extraction.py`, `evals/accuracy.py`
- Create: `evals/test_dates.py`

**Interfaces:**
- Consumes: `score_dates`, `score_match`, `Fixture`, `extract_dates`, `rank_matches`.
- Produces: `date_report(results_by_fixture: dict) -> str`.

- [ ] **Step 1: Write the eval**

```python
# evals/test_dates.py
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
from sqlalchemy.orm import sessionmaker

from evals.accuracy import baseline_path, date_report, load_fixtures, score_dates
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.dates.service import extract_dates
from renewal.models import DocumentText
from renewal.pipeline import ingest_document
from renewal.pdftext import PageText
from renewal.providers import build_client

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
```

- [ ] **Step 2: Add the report function**

```python
# evals/accuracy.py — append

def date_report(results_by_fixture: dict[str, dict]) -> str:
    """Recall first, and per fixture. It is the number that decides whether
    this feature is safe to rely on."""
    lines = ["", "date extraction:"]
    recalls, precisions = [], []
    for fixture_id, entry in sorted(results_by_fixture.items()):
        score = entry["score"]
        recalls.append(score["recall"])
        precisions.append(score["precision"])
        lines.append(
            f"  {fixture_id:<28} recall {100 * score['recall']:5.1f}%  "
            f"precision {100 * score['precision']:5.1f}%  "
            f"(tp {score['tp']}, fp {score['fp']}, fn {score['fn']})"
        )
    if recalls:
        lines.append("")
        lines.append(f"  overall recall    {100 * sum(recalls) / len(recalls):5.1f}%")
        lines.append(
            f"  overall precision {100 * sum(precisions) / len(precisions):5.1f}%"
        )
    return "\n".join(lines)
```

`DateScore` is a frozen dataclass, so `vars()` is the whole conversion to the
JSON-serializable shape the baseline file stores.

- [ ] **Step 3: Wire client matching into the same run**

Client resolution ships in this plan, so its scorer runs here too rather than
waiting for Plan C. In `evals/test_dates.py`, after ingesting each fixture, add:

```python
        from renewal.resolve.service import candidates_for
        ranked = [m.client_id for m in candidates_for(session, document)]
        expected_id = None
        if fixture.expected_client:
            expected_id = session.scalar(
                select(Client.id).where(Client.display_name == fixture.expected_client)
            )
        results[fixture.fixture_id]["match"] = vars(score_match(expected_id, ranked))
```

and print a match line per fixture beside the date line: top-1 and recall@5,
reported separately so a wrong auto-link and a merely bad candidate list are
distinguishable. Import `select`, `Client`, and `score_match` at the top.

Classification scoring stays unwired until Plan C, which is where the
classifier is built.

- [ ] **Step 4: Run the harness against the fixtures**

Run: `pytest -m eval evals/test_dates.py -s`
Expected: the report prints. With no fixture PDFs present, it skips — that is
correct, and the skip message says why.

- [ ] **Step 5: Commit**

```bash
git add evals/test_dates.py evals/accuracy.py
git commit -m "feat(evals): report date recall, precision, and client match quality"
```

---

## Task 28: Privacy, redaction, and the README

**Files:**
- Create: `scripts/redact.py`
- Modify: `PRIVACY.md`, `README.md`, `.gitignore`
- Test: `tests/test_redact.py` (create)

**Interfaces:**
- Consumes: `read_pdf`, `tests/pdfmaker.make_text_pdf`.
- Produces: `redact_text(text: str, *, substitutions: dict[str, str]) -> str`, `redact_pdf(data: bytes, *, substitutions: dict[str, str]) -> bytes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_redact.py
from renewal.pdftext import read_pdf
from scripts.redact import redact_pdf, redact_text
from tests.pdfmaker import make_text_pdf

SUBS = {"Acme Landscaping LLC": "Example Client LLC", "CAP-7781-22": "POL-0000-00"}


def test_named_values_are_replaced():
    got = redact_text("Named Insured: Acme Landscaping LLC", substitutions=SUBS)
    assert "Acme" not in got
    assert "Example Client LLC" in got


def test_vins_are_replaced_without_being_named():
    got = redact_text("VIN 1HGCM82633A004352", substitutions={})
    assert "1HGCM82633A004352" not in got


def test_ssn_shaped_numbers_are_replaced_without_being_named():
    got = redact_text("Tax ID 123-45-6789", substitutions={})
    assert "123-45-6789" not in got


def test_layout_survives_regeneration():
    data = make_text_pdf([["Named Insured: Acme Landscaping LLC",
                           "Policy Number: CAP-7781-22"]])
    out = redact_pdf(data, substitutions=SUBS)
    text = read_pdf(out).pages[0].text
    assert "Example Client LLC" in text
    assert "POL-0000-00" in text
    assert "Acme" not in text


def test_page_count_is_preserved():
    data = make_text_pdf([["one"], ["two"]])
    assert read_pdf(redact_pdf(data, substitutions={})).page_count == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_redact.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.redact'`

- [ ] **Step 3: Implement**

```python
# scripts/redact.py
"""Build eval fixtures from real documents.

Reads a real PDF's text, substitutes identifying values, and regenerates a
synthetic PDF from the result. Redacting a PDF in place is unreliable —
covered text stays in the content stream — so this regenerates instead. The
output is not a faithful copy of the original's layout, and it is not meant to
be: it is a fixture.

Named substitutions are explicit. Pattern-based ones catch what a human would
forget: VINs, tax-id-shaped numbers, and long digit runs.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import fitz

from renewal.pdftext import read_pdf

_PATTERNS = (
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "1XXXXXXXXXXXXXXXX"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "000-00-0000"),
    (re.compile(r"\b\d{9,}\b"), "000000000"),
)


def redact_text(text: str, *, substitutions: dict[str, str]) -> str:
    for old, new in substitutions.items():
        text = text.replace(old, new)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_pdf(data: bytes, *, substitutions: dict[str, str]) -> bytes:
    out = fitz.open()
    for page in read_pdf(data).pages:
        new = out.new_page()
        y = 72
        for line in redact_text(page.text, substitutions=substitutions).splitlines():
            new.insert_text((72, y), line, fontsize=11)
            y += 16
    return out.tobytes()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--substitutions", type=Path,
        help="JSON object mapping real values to replacements",
    )
    args = parser.parse_args()
    subs = json.loads(args.substitutions.read_text()) if args.substitutions else {}
    args.destination.write_bytes(
        redact_pdf(args.source.read_bytes(), substitutions=subs)
    )
    print(f"wrote {args.destination}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Update PRIVACY.md**

Add sections covering, in plain language:

- **What is stored.** Every document's bytes, its per-page text, extracted dates with their source text, and inbound message bodies. The whole book, not two files.
- **OCR is local.** Scanned pages are read by Tesseract on the host. No page image is transmitted to a model provider for text extraction, whatever `PROVIDER` is set to. The existing table of provider destinations still governs structured extraction, drafting, and the date-typing pass.
- **What the model sees for dates.** The first `DATE_PAGES` pages of text of every document go to `DATE_MODEL`. State that this is every document, not only the ones selected for structured extraction, because it is a larger exposure than the previous phase's.
- **Encryption at rest.** AES-GCM with `BLOB_ENCRYPTION_KEY`, and the limit stated plainly: the key lives beside the data, so this protects a stolen backup, a copied blob directory, or a decommissioned disk, and does not protect against a compromised host. If the variable is unset, blobs are stored unencrypted and the application logs a warning at startup.
- **The .ics token.** Anyone holding the feed URL can read every client name and deadline. It is regenerable from `/settings`, which immediately breaks the old link.
- **Retention.** Nothing is deleted automatically. Blobs, text, and dates persist until removed by hand.

- [ ] **Step 5: Update README.md**

Add a "System dependencies" section (Tesseract, with per-distro install lines and the consequence of omitting it), a "Bulk import" section showing `python -m scripts.bulk_import /path/to/archive`, and a "Blob encryption" section showing how to generate a key and how to seal an existing store with `python -m scripts.encrypt_blobs`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_redact.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add scripts/redact.py PRIVACY.md README.md .gitignore tests/test_redact.py
git commit -m "docs: privacy for the record layer, and a fixture redaction helper"
```

---

## Stop here

This is the gate the spec names. Before Plan B begins:

- [ ] Run the full suite: `pytest tests/ -v`. Every test passes.
- [ ] Import her real archive with `python -m scripts.bulk_import`, and confirm the counts are plausible against the folder tree.
- [ ] Check the log output contains no document content — ids, hashes, counts, and statuses only.
- [ ] Put `/calendar` in front of her against that archive.
- [ ] Record what she says about recall: which dates are missing, which spurious ones are annoying, and whether escalation fires when it should.

Her feedback reshapes Plans B and C. Do not start them before this happens.
