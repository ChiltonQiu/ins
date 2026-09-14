# Comparison Matrix and Call Prep Sheet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the comparison engine N-way so an incumbent renewal can be set against the alternatives she has been quoted, wire the record layer into the comparison path, and give the client page a sheet she reads from on the phone.

**Architecture:** `difference` stays the row table; `comparison_column` and `difference_cell` hang off it, so a two-term renewal diff is literally a two-column matrix. `renewal/diff.py` gains a pure `diff_field_sets`; `renewal/comparison.py` gains `build_matrix` (the one write path) and `matrix_for` (the one read path, which also synthesizes columns for comparisons written before this change). Quotes are `PolicyTerm` rows with `kind='quoted'` on the incumbent's chain. Carrier-specific fields land in `policy_term_extra`, typed by `config/extras.yaml`. Promotion gets wired into `pipeline.py` so a renewal that arrives by email can become a term.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 `Mapped`/`mapped_column`, Alembic, Jinja2, Postgres, pytest + `TestClient`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-12-comparison-matrix-design.md`

## Global Constraints

- **Do not edit these five files.** They are where the pressure to edit will be, and each has a reason it holds:
  - `renewal/premium.py` — attribution stays a two-map function; the matrix calls it per comparand (D5).
  - `renewal/materiality.py` — `classify()` takes the diff's `RawDifference`, not the ORM row, so the matrix synthesizes one per pair (D3).
  - `renewal/extract/prompt_v1.py`, `renewal/extract/schema.py`, `renewal/extract/validate.py` — extras arrive as human corrections, not from the model (D9). Touching the prompt means a new extractor version, a re-extraction of the corpus, and a fresh eval baseline per provider and model.
- **`difference.prior_value` and `renewal_value` are never written again.** They stay on the table because they are the only record of what the pre-existing comparisons compared. New rows leave them NULL and use cells.
- **A comparison with no `comparison_column` rows is legacy.** That absence is the only marker; there is no status column and nothing is backfilled. `matrix_for()` is the one place that knows.
- **Exactly one baseline per comparison, at position 0.** Enforced by a partial unique index in the database and by `build_matrix` raising. Every other column is a comparand and is measured against it.
- **Nothing is ranked.** No sort by premium, no "best" marker, no recommendation, anywhere — not in the engine, not in a template, not in a draft (D2).
- **Only classify comparands that actually differ from the baseline.** Most rules match on path alone (`deductible_change` has no `when`), so classifying an equal pair would return `material` for a cell that did not move. This is the single easiest bug to write in this plan.
- **Attribution reads `kind`, not `policy_id`.** A quote hangs off the incumbent's chain deliberately, so `policy_id` is equal by construction and cannot discriminate (D5).
- **`MAX_COLUMNS = 5`** — a baseline and four comparands. A module constant in `renewal/comparison.py`, not a setting: it is a property of what fits on screen from 390px up, not of an installation.
- **Insert-only still holds.** Every new table here is written once at build and never updated. Reclassification keeps logging against `difference`.
- **Task 3 fixes the signatures of `build_matrix` and `matrix_for` for good.** Both take `settings` from the moment they are written, even though nothing reads it until Task 6 (`extras_config`) and Task 9 (`attention_premium_pct`). This is deliberate: adding a parameter in a later task means re-editing an earlier task's output and every one of its four call sites — `web/review.py`, the two in `web/comparison.py`, and `clients/prep.py`. `settings=None` means neither later behaviour runs, which is what the tests that build a matrix without settings rely on.
- **`renewal/comparison.py` imports `renewal/attention/rules.py`, never the reverse.** `evaluate_comparison` takes plain values rather than a `Matrix` so that edge stays one-way.
- **Do not touch** `Correction`, `DateEvent`, `AttentionEvent`, `ManualDateEvent`, or add actor columns anywhere. Attribution of who acted is still deferred.

---

### Task 1: The three migrations

**Files:**
- Modify: `renewal/models.py` — three new classes, `PolicyTerm.kind`, three `Comparison` columns become nullable
- Create: `migrations/versions/<generated>_comparison_matrix.py`
- Create: `migrations/versions/<generated>_policy_term_kind.py`
- Create: `migrations/versions/<generated>_policy_term_extra.py`
- Modify: `conftest.py:48-55` — the `TABLES` tuple
- Test: `tests/test_models_matrix.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `renewal.models.ComparisonColumn` — `id, comparison_id, position, policy_term_id, role, created_at`
  - `renewal.models.DifferenceCell` — `id, difference_id, comparison_column_id, value`
  - `renewal.models.PolicyTermExtra` — `id, policy_term_id, field_path, value, created_at`
  - `renewal.models.PolicyTerm.kind` — `'bound' | 'quoted'`, default `'bound'`
  - `Comparison.prior_term_id`, `renewal_term_id`, `renewal_run_id` — all nullable

Three migrations rather than one, one concern each, per the spec. They are in one task because a half-migrated schema is not a state anything can be shown working in.

- [x] **Step 1: Write the failing test**

Create `tests/test_models_matrix.py`:

```python
"""The matrix tables.

Insert-only like the rest of the record: a comparison and its columns and
cells are written once at build and never updated. Reclassifying still logs
against the difference row, which is why difference stays the row table.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from renewal.models import (
    Client, Comparison, ComparisonColumn, Difference, DifferenceCell, Policy,
    PolicyTerm, PolicyTermExtra,
)


def _policy(session):
    client = Client(display_name="Acme Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id, carrier_name="Progressive",
        policy_number="PA-1", line_of_business="commercial_auto",
    )
    session.add(policy)
    session.flush()
    return policy


def _term(session, policy, **kwargs):
    term = PolicyTerm(policy_id=policy.id, **kwargs)
    session.add(term)
    session.flush()
    return term


def test_a_term_is_bound_unless_it_says_otherwise(session):
    """Every term written before this change was a bound policy term, and the
    default records that rather than assuming it."""
    policy = _policy(session)
    assert _term(session, policy).kind == "bound"
    assert _term(session, policy, kind="quoted").kind == "quoted"


def test_a_term_kind_is_one_of_two_words(session):
    policy = _policy(session)
    with pytest.raises(IntegrityError):
        _term(session, policy, kind="maybe")


def test_a_comparison_needs_no_run_and_no_term_ids(session):
    """A comparison assembled from the record has no upload pair behind it.
    Inventing a RenewalRun for it would make the run table lie."""
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    assert comparison.id
    assert comparison.renewal_run_id is None
    assert comparison.prior_term_id is None


def test_columns_are_positional_and_unique(session):
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    first = ComparisonColumn(
        comparison_id=comparison.id, position=0,
        policy_term_id=_term(session, policy).id, role="baseline",
    )
    session.add(first)
    session.flush()
    session.add(ComparisonColumn(
        comparison_id=comparison.id, position=0,
        policy_term_id=_term(session, policy).id, role="comparand",
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_comparison_has_at_most_one_baseline(session):
    """Enforced in the database rather than only in build_matrix. Two
    baselines would mean every cell was measured against something different
    depending on which row the reader looked at first."""
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    for position in (0, 1):
        session.add(ComparisonColumn(
            comparison_id=comparison.id, position=position,
            policy_term_id=_term(session, policy).id, role="baseline",
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_cell_holds_a_null_for_a_field_the_column_lacks(session):
    """NULL means the field is not on that document at all, which is a
    different thing from an empty string and renders differently."""
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    column = ComparisonColumn(
        comparison_id=comparison.id, position=0,
        policy_term_id=_term(session, policy).id, role="baseline",
    )
    difference = Difference(
        comparison_id=comparison.id, field_path="coverage.COMP.premium",
        materiality="material", rule_id="coverage_premium_change",
    )
    session.add_all([column, difference])
    session.flush()
    cell = DifferenceCell(
        difference_id=difference.id, comparison_column_id=column.id, value=None
    )
    session.add(cell)
    session.flush()
    assert cell.value is None


def test_one_cell_per_difference_and_column(session):
    policy = _policy(session)
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    column = ComparisonColumn(
        comparison_id=comparison.id, position=0,
        policy_term_id=_term(session, policy).id, role="baseline",
    )
    difference = Difference(
        comparison_id=comparison.id, field_path="policy.total_premium",
        materiality="material", rule_id="premium_total_change",
    )
    session.add_all([column, difference])
    session.flush()
    for value in ("3900.00", "4210.00"):
        session.add(DifferenceCell(
            difference_id=difference.id, comparison_column_id=column.id,
            value=value,
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_difference_row_per_field_path(session):
    comparison = Comparison()
    session.add(comparison)
    session.flush()
    for _ in range(2):
        session.add(Difference(
            comparison_id=comparison.id, field_path="policy.total_premium",
            materiality="material", rule_id="premium_total_change",
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_extra_is_unique_per_term_and_path(session):
    """A term is written once at promotion, so a second value for one path
    would mean promotion ran twice into the same row."""
    policy = _policy(session)
    term = _term(session, policy)
    for _ in range(2):
        session.add(PolicyTermExtra(
            policy_term_id=term.id, field_path="extras.surcharge_total",
            value="120.00",
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_extra_carries_no_type(session):
    """The type belongs to the key, not the term, and lives in
    config/extras.yaml. A column here would be one fact copied into many rows
    that can disagree with each other and with the config."""
    assert not hasattr(PolicyTermExtra, "value_type")
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_models_matrix.py -v`
Expected: FAIL — `ImportError: cannot import name 'ComparisonColumn' from 'renewal.models'`

- [x] **Step 3: Write the models**

In `renewal/models.py`, add `kind` to `PolicyTerm` immediately after `policy_id`:

```python
    kind: Mapped[str] = mapped_column(Text, server_default="bound")
```

and give `PolicyTerm` a `__table_args__`:

```python
    __table_args__ = (
        CheckConstraint("kind IN ('bound', 'quoted')", name="ck_policy_term_kind"),
    )
```

Extend the class docstring, because the meaning of the table has widened:

```python
class PolicyTerm(Base):
    """A frozen snapshot promoted from one extraction plus the corrections
    standing at that moment. Never updated: a later correction promotes a new
    row.

    kind='quoted' is a competitor's offer for the same risk, hanging off the
    incumbent's policy chain so that carrier_name means what it has always
    meant — what that term's document said. Every query that means "the
    current term" must filter kind='bound'; there is exactly one such query,
    _latest_term() in renewal/clients/overview.py.
    """
```

Make the three `Comparison` columns nullable and say why:

```python
class Comparison(Base):
    """One baseline column and up to four comparands, in comparison_column.

    prior_term_id and renewal_term_id are not written any more — the same term
    ids live on the columns. They stay because for every comparison built
    before the matrix they are the only record of what was compared, and
    matrix_for() reads them to render those rows. renewal_run_id is NULL for a
    comparison assembled from the record rather than from an upload pair.
    """

    __tablename__ = "comparison"
    id: Mapped[int] = mapped_column(primary_key=True)
    renewal_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("renewal_run.id"), nullable=True
    )
    prior_term_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_term.id"), nullable=True
    )
    renewal_term_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_term.id"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    differences: Mapped[list["Difference"]] = relationship(back_populates="comparison")
```

Add a `UniqueConstraint` to `Difference.__table_args__`, keeping the existing check:

```python
    __table_args__ = (
        CheckConstraint(
            "materiality IN ('material', 'informational', 'noise')",
            name="ck_difference_materiality",
        ),
        UniqueConstraint(
            "comparison_id", "field_path", name="uq_difference_path"
        ),
    )
```

Add the three new classes after `Draft`:

```python
class ComparisonColumn(Base):
    """One column of a comparison. Exactly one row per comparison is the
    baseline and everything else is measured against it; the partial unique
    index in the migration is what enforces that, because a CHECK cannot see
    across rows.

    A comparison with no rows here was built before the matrix and is read
    through Comparison.prior_term_id and renewal_term_id instead. Absence is
    the marker: no status column, and nothing backfilled.
    """

    __tablename__ = "comparison_column"
    __table_args__ = (
        CheckConstraint(
            "role IN ('baseline', 'comparand')", name="ck_comparison_column_role"
        ),
        UniqueConstraint(
            "comparison_id", "position", name="uq_comparison_column_position"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("comparison.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    policy_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class DifferenceCell(Base):
    """One column's value for one difference row.

    value NULL means the field is absent from that column — not on that
    document at all. That is a different thing from an empty string and it is
    usually why a cheaper quote is cheaper, so it renders as words rather than
    as a blank cell.
    """

    __tablename__ = "difference_cell"
    __table_args__ = (
        UniqueConstraint(
            "difference_id", "comparison_column_id", name="uq_difference_cell"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    difference_id: Mapped[int] = mapped_column(ForeignKey("difference.id"), index=True)
    comparison_column_id: Mapped[int] = mapped_column(
        ForeignKey("comparison_column.id")
    )
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class PolicyTermExtra(Base):
    """A carrier-specific field, kept out of the promoted columns.

    No value_type column: the type belongs to the key rather than to the term
    and lives in config/extras.yaml, so there is one copy of it instead of one
    per row.
    """

    __tablename__ = "policy_term_extra"
    __table_args__ = (
        UniqueConstraint(
            "policy_term_id", "field_path", name="uq_policy_term_extra"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_term_id: Mapped[int] = mapped_column(
        ForeignKey("policy_term.id"), index=True
    )
    field_path: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
```

- [x] **Step 4: Write the three migrations**

Generate each with `alembic revision -m "..."` and fill it in by hand — autogenerate will try to bundle all three and will miss the partial index.

Migration A, `comparison_matrix`:

```python
def upgrade() -> None:
    # The diff has always emitted one row per field path. Assert it before
    # relying on it, so a duplicate surfaces here rather than as a constraint
    # violation on some later build.
    duplicates = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM ("
        "  SELECT comparison_id, field_path FROM difference"
        "  GROUP BY comparison_id, field_path HAVING count(*) > 1"
        ") d"
    )).scalar()
    assert not duplicates, f"{duplicates} duplicate (comparison_id, field_path)"

    op.create_table(
        "comparison_column",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("comparison_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("policy_term_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint("role IN ('baseline', 'comparand')",
                           name="ck_comparison_column_role"),
        sa.ForeignKeyConstraint(["comparison_id"], ["comparison.id"]),
        sa.ForeignKeyConstraint(["policy_term_id"], ["policy_term.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("comparison_id", "position",
                            name="uq_comparison_column_position"),
    )
    op.create_index("ix_comparison_column_comparison_id", "comparison_column",
                    ["comparison_id"])
    # A CHECK cannot see across rows, so one baseline per comparison is a
    # partial unique index. Same technique as uq_app_user_email_lower.
    op.create_index(
        "uq_comparison_one_baseline", "comparison_column", ["comparison_id"],
        unique=True, postgresql_where=sa.text("role = 'baseline'"),
    )

    op.create_table(
        "difference_cell",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("difference_id", sa.Integer(), nullable=False),
        sa.Column("comparison_column_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["difference_id"], ["difference.id"]),
        sa.ForeignKeyConstraint(["comparison_column_id"],
                                ["comparison_column.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("difference_id", "comparison_column_id",
                            name="uq_difference_cell"),
    )
    op.create_index("ix_difference_cell_difference_id", "difference_cell",
                    ["difference_id"])

    op.create_unique_constraint("uq_difference_path", "difference",
                                ["comparison_id", "field_path"])

    for column in ("renewal_run_id", "prior_term_id", "renewal_term_id"):
        op.alter_column("comparison", column, existing_type=sa.Integer(),
                        nullable=True)
```

Migration B, `policy_term_kind`:

```python
def upgrade() -> None:
    # Every term written before this migration was a bound policy term. The
    # server default records that rather than leaving it to be assumed.
    op.add_column("policy_term", sa.Column(
        "kind", sa.Text(), nullable=False, server_default="bound"
    ))
    op.create_check_constraint(
        "ck_policy_term_kind", "policy_term", "kind IN ('bound', 'quoted')"
    )
```

Migration C, `policy_term_extra`:

```python
def upgrade() -> None:
    op.create_table(
        "policy_term_extra",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("policy_term_id", sa.Integer(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["policy_term_id"], ["policy_term.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_term_id", "field_path",
                            name="uq_policy_term_extra"),
    )
    op.create_index("ix_policy_term_extra_policy_term_id", "policy_term_extra",
                    ["policy_term_id"])
```

Each `downgrade()` drops what its `upgrade()` created, in reverse.

- [x] **Step 5: Extend the truncate list**

In `conftest.py`, add the three tables to `TABLES`. Order does not matter — `CASCADE` handles the dependencies — but keep them next to the tables they hang off:

```python
TABLES = (
    "client, policy, policy_term, policy_term_extra, coverage, insured_item,"
    " document, extraction, extracted_field, correction, renewal_run,"
    " comparison, comparison_column, difference, difference_cell,"
    " reclassification, draft, carrier, carrier_alias, carrier_admitted_status,"
    " policy_billing_type, document_text, document_classification,"
    " document_link, document_date, date_event, manual_date, manual_date_event,"
    " inbound_message, attention_item, attention_event, app_user, user_session"
)
```

- [x] **Step 6: Migrate and run the test**

```bash
.venv/bin/alembic upgrade head
.venv/bin/pytest tests/test_models_matrix.py -v
```
Expected: PASS.

Then confirm nothing else moved: `.venv/bin/pytest`
Expected: PASS — the whole suite. This task changes no behaviour.

- [x] **Step 7: Commit**

```bash
git add renewal/models.py migrations/versions/ conftest.py \
        tests/test_models_matrix.py
git commit -m "feat(comparison): tables for an N-way comparison"
```

---

### Task 2: The N-way diff, pure

**Files:**
- Modify: `renewal/diff.py` — `FieldSet`, `MatrixRow`, `diff_field_sets`; `diff_terms` becomes a two-set caller
- Test: `tests/test_diff_matrix.py`

**Interfaces:**
- Consumes: `normalize`, `term_field_map` from `renewal/diff.py`, unchanged.
- Produces:
  - `FieldSet(term_id: int, values: dict[str, str | None])`
  - `MatrixRow(field_path: str, baseline_value: str | None, comparand_values: list[str | None])`
  - `diff_field_sets(baseline: FieldSet, comparands: list[FieldSet]) -> list[MatrixRow]`

`tests/test_diff.py` is **not** edited. `diff_terms` keeps its signature and its return type, and that file passing unedited is the evidence that the two-term case really is the special case.

- [x] **Step 1: Write the failing test**

Create `tests/test_diff_matrix.py`:

```python
"""Diffing N field sets against one baseline.

Pure: no session, no persistence, no rules. A path becomes a row when any
comparand disagrees with the baseline, and absence on one side is a
disagreement like any other.
"""

from renewal.diff import FieldSet, diff_field_sets


def _set(term_id, **values):
    return FieldSet(term_id=term_id, values=values)


def test_a_path_every_column_agrees_on_is_not_a_row():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "3900.00"}),
        [_set(2, **{"policy.total_premium": "3900.00"}),
         _set(3, **{"policy.total_premium": "3900.00"})],
    )
    assert rows == []


def test_one_disagreeing_comparand_makes_a_row_carrying_all_of_them():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "3900.00"}),
        [_set(2, **{"policy.total_premium": "3900.00"}),
         _set(3, **{"policy.total_premium": "4455.00"})],
    )
    assert len(rows) == 1
    assert rows[0].baseline_value == "3900.00"
    assert rows[0].comparand_values == ["3900.00", "4455.00"]


def test_comparand_values_stay_positional():
    """The list index is the column. A comparand that has nothing to say about
    a path holds None at its own position rather than being left out."""
    rows = diff_field_sets(
        _set(1, **{"coverage.BI.limit_value": "100/300"}),
        [_set(2), _set(3, **{"coverage.BI.limit_value": "50/100"})],
    )
    assert rows[0].comparand_values == [None, "50/100"]


def test_a_path_only_a_comparand_has_is_a_row():
    rows = diff_field_sets(
        _set(1),
        [_set(2, **{"extras.surcharge_total": "120.00"})],
    )
    assert rows[0].field_path == "extras.surcharge_total"
    assert rows[0].baseline_value is None


def test_normalisation_still_only_canonicalises_type():
    """$1,200 and 1200.00 are the same premium and not a row. This is the
    existing normalize(), reached through the new entry point."""
    rows = diff_field_sets(
        _set(1, **{"coverage.COMP.premium": "$1,200.00"}),
        [_set(2, **{"coverage.COMP.premium": "1200"})],
    )
    assert rows == []


def test_rows_come_back_in_path_order():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "1", "coverage.BI.limit_value": "1"}),
        [_set(2, **{"policy.total_premium": "2", "coverage.BI.limit_value": "2"})],
    )
    assert [r.field_path for r in rows] == [
        "coverage.BI.limit_value", "policy.total_premium",
    ]


def test_one_comparand_is_the_pairwise_case():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "3900.00"}),
        [_set(2, **{"policy.total_premium": "4210.00"})],
    )
    assert len(rows) == 1
    assert rows[0].comparand_values == ["4210.00"]


def test_no_comparands_is_no_rows():
    """Not a state the picker can produce, but the engine must not raise on
    it: an empty comparison is empty, not an error."""
    assert diff_field_sets(_set(1, **{"policy.total_premium": "1"}), []) == []
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_diff_matrix.py -v`
Expected: FAIL — `ImportError: cannot import name 'FieldSet' from 'renewal.diff'`

- [x] **Step 3: Write the implementation**

In `renewal/diff.py`, add after `RawDifference`:

```python
@dataclass(frozen=True)
class FieldSet:
    """One term, flattened. The engine takes these rather than terms so that
    it needs no session and the pairwise case is the same call."""

    term_id: int
    values: dict[str, str | None]


@dataclass(frozen=True)
class MatrixRow:
    """comparand_values is positional: index i is the i-th comparand, and None
    means that column does not have the field at all."""

    field_path: str
    baseline_value: str | None
    comparand_values: list[str | None]


def diff_field_sets(
    baseline: FieldSet, comparands: list[FieldSet]
) -> list[MatrixRow]:
    """Every path any column mentions, kept when any comparand disagrees with
    the baseline. Nothing is suppressed here — classification labels rows
    later, exactly as in the pairwise case."""
    paths: set[str] = set(baseline.values)
    for comparand in comparands:
        paths |= set(comparand.values)

    rows = []
    for path in sorted(paths):
        before = baseline.values.get(path)
        after = [comparand.values.get(path) for comparand in comparands]
        canonical = normalize(path, before)
        if any(normalize(path, value) != canonical for value in after):
            rows.append(MatrixRow(path, before, after))
    return rows
```

Rewrite `diff_terms` as the two-set caller, keeping its signature and its
return type:

```python
def diff_terms(
    session: Session, prior: PolicyTerm, renewal: PolicyTerm
) -> list[RawDifference]:
    """The pairwise case, which is one comparand against one baseline."""
    rows = diff_field_sets(
        FieldSet(prior.id, term_field_map(session, prior)),
        [FieldSet(renewal.id, term_field_map(session, renewal))],
    )
    return [
        RawDifference(row.field_path, row.baseline_value, row.comparand_values[0])
        for row in rows
    ]
```

- [x] **Step 4: Run both diff test files**

Run: `.venv/bin/pytest tests/test_diff_matrix.py tests/test_diff.py -v`
Expected: PASS. `tests/test_diff.py` passes **unedited** — that is the point of this task.

- [x] **Step 5: Commit**

```bash
git add renewal/diff.py tests/test_diff_matrix.py
git commit -m "feat(diff): compare N field sets against one baseline"
```

---
### Task 3: The write path, the read path, and the draft

**Files:**
- Modify: `renewal/comparison.py` — `MAX_COLUMNS`, `ColumnSpec`, `ColumnsRejected`, `build_matrix`, `matrix_for`; `build_comparison` becomes a two-column caller; `breakdown_for` is removed
- Modify: `renewal/draft.py` — `build_prompt` and `generate_draft` read a `Matrix`
- Modify: `renewal/web/review.py` — the promote path
- Rewrite: `tests/test_comparison.py:114-116`, `tests/test_draft.py:81-102`
- Test: `tests/test_matrix.py`

**Interfaces:**
- Consumes: `diff_field_sets`, `FieldSet`, `term_field_map`, `normalize` (Task 2); `classify` and `RuleSet` from `renewal/materiality.py`, unedited; `attribute_premium` from `renewal/premium.py`, unedited.
- Produces:
  - `MAX_COLUMNS = 5`
  - `ColumnSpec(policy_term_id: int, role: str)`
  - `ColumnsRejected(Exception)` — carries the reason as its message
  - `build_matrix(session, *, columns, rules, run_id=None) -> Comparison`
  - `matrix_for(session, comparison, *, include_noise=True) -> Matrix`
  - `Matrix`, `Column`, `Row`, `Cell` dataclasses

This is the seam task. After it there is exactly one way a comparison is written and one shape it is read in, and the pairwise rows already in the database arrive through that shape looking like everything else.

- [x] **Step 1: Write the failing test**

Create `tests/test_matrix.py`:

```python
"""Building and reading an N-way comparison.

One write path: build_matrix. One read path: matrix_for, which also
synthesizes columns and cells for the comparisons written before the matrix
existed, so nothing downstream has to know which kind it is holding.
"""

from decimal import Decimal

import pytest

from renewal.comparison import (
    MAX_COLUMNS, ColumnSpec, ColumnsRejected, build_matrix, matrix_for,
)
from renewal.materiality import load_rules
from renewal.models import (
    Client, Comparison, Coverage, Difference, Policy, PolicyTerm,
)

RULES = load_rules("config/materiality.yaml")


def _client_policy(session, name="Acme Landscaping"):
    client = Client(display_name=name)
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id, carrier_name="Progressive", policy_number="PA-1",
        line_of_business="commercial_auto", state="OR",
    )
    session.add(policy)
    session.flush()
    return policy


def _term(session, policy, *, premium, carrier="Progressive", kind="bound",
          comp_deductible=None):
    term = PolicyTerm(
        policy_id=policy.id, kind=kind, carrier_name=carrier,
        policy_number="PA-1", total_premium=premium,
    )
    session.add(term)
    session.flush()
    if comp_deductible is not None:
        session.add(Coverage(
            policy_term_id=term.id, coverage_code="COMP",
            deductible_value=comp_deductible, premium="400.00",
        ))
        session.flush()
    return term


def test_a_two_column_matrix_carries_the_terms_on_its_columns(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="4210.00")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"),
                 ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert [c.term.id for c in matrix.columns] == [prior.id, renewal.id]
    assert [c.role for c in matrix.columns] == ["baseline", "comparand"]
    assert not matrix.legacy


def test_the_pairwise_columns_are_not_written(session):
    """The same two term ids live on the columns. A second copy on the
    comparison could disagree with them."""
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
                 ColumnSpec(_term(session, policy, premium="2").id, "comparand")],
        rules=RULES,
    )
    assert comparison.prior_term_id is None
    assert comparison.renewal_term_id is None


def test_a_third_column_adds_a_cell_not_a_row(session):
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00")
    b = _term(session, policy, premium="3880.00", carrier="Carrier B",
              kind="quoted")
    c = _term(session, policy, premium="4455.00", carrier="Carrier C",
              kind="quoted")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(baseline.id, "baseline"),
                 ColumnSpec(b.id, "comparand"), ColumnSpec(c.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    row = next(r for r in matrix.rows
               if r.difference.field_path == "policy.total_premium")
    assert row.baseline.value == "4210.00"
    assert [cell.value for cell in row.comparands] == ["3880.00", "4455.00"]


def test_a_field_a_comparand_lacks_is_a_null_cell(session):
    """Not an empty string. It is usually why the cheaper quote is cheaper."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00", comp_deductible="500")
    quote = _term(session, policy, premium="3880.00", carrier="Carrier B",
                  kind="quoted")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(baseline.id, "baseline"),
                 ColumnSpec(quote.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    row = next(r for r in matrix.rows
               if r.difference.field_path == "coverage.COMP.deductible_value")
    assert row.baseline.value == "500"
    assert row.comparands[0].value is None


def test_the_strongest_comparand_sets_the_row(session):
    """Carrier B matches the incumbent's deductible and Carrier C does not.
    The row is material because under-flagging is the dangerous direction."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00", comp_deductible="500")
    b = _term(session, policy, premium="4210.00", kind="quoted",
              carrier="Carrier B", comp_deductible="500")
    c = _term(session, policy, premium="4210.00", kind="quoted",
              carrier="Carrier C", comp_deductible="1000")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(baseline.id, "baseline"),
                 ColumnSpec(b.id, "comparand"), ColumnSpec(c.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    row = next(r for r in matrix.rows
               if r.difference.field_path == "coverage.COMP.deductible_value")
    assert row.difference.materiality == "material"
    assert row.difference.rule_id == "deductible_change"
    assert row.comparands[0].differs is False
    assert row.comparands[1].differs is True


def test_a_matching_comparand_is_not_classified(session):
    """deductible_change matches on path with no `when`, so classifying a pair
    that did not move would return material for a cell that is identical to
    the baseline. Only differing comparands are put to the rules."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00", comp_deductible="500")
    b = _term(session, policy, premium="4210.00", kind="quoted",
              carrier="Carrier B", comp_deductible="500")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(baseline.id, "baseline"),
                 ColumnSpec(b.id, "comparand")],
        rules=RULES,
    )
    paths = [r.difference.field_path for r in matrix_for(session, comparison).rows]
    assert "coverage.COMP.deductible_value" not in paths


def test_attribution_runs_between_two_bound_terms(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="4210.00", comp_deductible="1000")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"),
                 ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.columns[1].total_delta == Decimal("310.00")
    assert matrix.columns[1].breakdown.available
    assert matrix.columns[1].breakdown_reason is None


def test_every_column_carries_its_own_total(session):
    """The baseline has no delta but does have a total: the premium_change
    rule needs it as the denominator and the prep sheet prints it as the
    'was' figure."""
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="4210.00")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"),
                 ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.columns[0].total == Decimal("3900.00")
    assert matrix.columns[0].total_delta is None
    assert matrix.columns[1].total == Decimal("4210.00")


def test_attribution_is_skipped_for_a_quoted_column(session):
    """Different carriers are different coverage-code vocabularies, so almost
    the whole delta would land in the residual and the breakdown would look
    like an analysis. The total delta is still arithmetic and still shown."""
    policy = _client_policy(session)
    baseline = _term(session, policy, premium="4210.00")
    quote = _term(session, policy, premium="3880.00", carrier="Carrier B",
                  kind="quoted")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(baseline.id, "baseline"),
                 ColumnSpec(quote.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.columns[1].total_delta == Decimal("-330.00")
    assert matrix.columns[1].breakdown is None
    assert "vocabular" in matrix.columns[1].breakdown_reason


def test_two_bound_terms_of_one_policy_are_draft_eligible(session):
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
                 ColumnSpec(_term(session, policy, premium="2").id, "comparand")],
        rules=RULES,
    )
    assert matrix_for(session, comparison).draft_eligible


def test_a_quoted_column_is_not_draft_eligible(session):
    """Any wording that sets carriers side by side is a recommendation."""
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
            ColumnSpec(_term(session, policy, premium="2", kind="quoted").id,
                       "comparand"),
        ],
        rules=RULES,
    )
    assert not matrix_for(session, comparison).draft_eligible


def test_three_bound_columns_are_not_draft_eligible(session):
    """A term history has no single 'what changed' to explain."""
    policy = _client_policy(session)
    comparison = build_matrix(
        session,
        columns=[
            ColumnSpec(_term(session, policy, premium="1").id, "baseline"),
            ColumnSpec(_term(session, policy, premium="2").id, "comparand"),
            ColumnSpec(_term(session, policy, premium="3").id, "comparand"),
        ],
        rules=RULES,
    )
    assert not matrix_for(session, comparison).draft_eligible


def test_a_comparison_from_before_the_matrix_still_reads(session):
    """No comparison_column rows, so the two term ids and the two value
    columns are what it has. Absence is the only marker."""
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="4210.00")
    comparison = Comparison(prior_term_id=prior.id, renewal_term_id=renewal.id)
    session.add(comparison)
    session.flush()
    session.add(Difference(
        comparison_id=comparison.id, field_path="policy.total_premium",
        prior_value="3900.00", renewal_value="4210.00",
        materiality="material", rule_id="premium_total_change",
    ))
    session.flush()

    matrix = matrix_for(session, comparison)
    assert matrix.legacy
    assert [c.term.id for c in matrix.columns] == [prior.id, renewal.id]
    assert matrix.rows[0].baseline.value == "3900.00"
    assert matrix.rows[0].comparands[0].value == "4210.00"
    assert matrix.rows[0].comparands[0].differs
    assert matrix.draft_eligible


def test_rows_are_ordered_material_first_then_by_path(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00", comp_deductible="500")
    renewal = _term(session, policy, premium="3901.00", comp_deductible="1000")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"),
                 ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    matrix = matrix_for(session, comparison)
    assert matrix.rows[0].difference.materiality == "material"


def test_noise_can_be_left_out(session):
    policy = _client_policy(session)
    prior = _term(session, policy, premium="3900.00")
    renewal = _term(session, policy, premium="3900.50")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"),
                 ColumnSpec(renewal.id, "comparand")],
        rules=RULES,
    )
    assert matrix_for(session, comparison, include_noise=True).rows
    assert not matrix_for(session, comparison, include_noise=False).rows


@pytest.mark.parametrize("reason,build", [
    ("at most", lambda p, t: [ColumnSpec(t(p).id, "baseline")]
     + [ColumnSpec(t(p).id, "comparand") for _ in range(MAX_COLUMNS)]),
    ("exactly one baseline", lambda p, t: [ColumnSpec(t(p).id, "comparand"),
                                           ColumnSpec(t(p).id, "comparand")]),
    ("first column", lambda p, t: [ColumnSpec(t(p).id, "comparand"),
                                   ColumnSpec(t(p).id, "baseline")]),
])
def test_the_picker_refusals_are_raised_where_the_rule_lives(
    session, reason, build
):
    policy = _client_policy(session)

    def make(p):
        return _term(session, p, premium="1")

    with pytest.raises(ColumnsRejected, match=reason):
        build_matrix(session, columns=build(policy, make), rules=RULES)


def test_a_quoted_baseline_is_refused(session):
    """Measuring the incumbent against a quote inverts what every other
    column means."""
    policy = _client_policy(session)
    quote = _term(session, policy, premium="1", kind="quoted")
    bound = _term(session, policy, premium="2")
    with pytest.raises(ColumnsRejected, match="bound term"):
        build_matrix(
            session,
            columns=[ColumnSpec(quote.id, "baseline"),
                     ColumnSpec(bound.id, "comparand")],
            rules=RULES,
        )


def test_columns_from_two_policies_are_refused(session):
    """The policy chain is what makes these the same risk."""
    first = _client_policy(session)
    second = _client_policy(session, name="Beta Freight")
    with pytest.raises(ColumnsRejected, match="one policy"):
        build_matrix(
            session,
            columns=[ColumnSpec(_term(session, first, premium="1").id, "baseline"),
                     ColumnSpec(_term(session, second, premium="2").id, "comparand")],
            rules=RULES,
        )
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_matrix.py -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_COLUMNS' from 'renewal.comparison'`

- [x] **Step 3: Rewrite `renewal/comparison.py`**

```python
"""Assembling a comparison from N frozen terms.

One baseline column and up to four comparands. A two-term renewal diff is the
two-column case rather than a different object, which is why difference is
still the row table and the cells hang off it.

The comparison, its columns, its rows and its cells are written once.
Reclassifying in the UI logs a reclassification row rather than editing the
difference — that log is the evidence for which rules are wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from renewal.carriers import admitted_status, resolve_carrier
from renewal.config import Settings
from renewal.diff import (
    FieldSet, MatrixRow, RawDifference, diff_field_sets, normalize,
    term_field_map,
)
from renewal.materiality import RuleSet, classify
from renewal.models import (
    Client, Comparison, ComparisonColumn, Difference, DifferenceCell, Policy,
    PolicyTerm, Reclassification,
)
from renewal.premium import PremiumBreakdown, attribute_premium

# A baseline and four comparands. A property of what fits on screen from 390px
# up, not of an installation, so it is a constant rather than a setting — the
# same reasoning that keeps RENEWAL_WINDOW_DAYS out of Settings.
MAX_COLUMNS = 5

_STRENGTH = {"material": 0, "informational": 1, "noise": 2}

_ACROSS_CARRIERS = (
    "line-item attribution is not offered against a quote: a different "
    "carrier is a different coverage-code vocabulary, so almost every line "
    "would fall into the residual"
)


@dataclass(frozen=True)
class ColumnSpec:
    policy_term_id: int
    role: str


class ColumnsRejected(Exception):
    """A refusal with its reason, raised where the rule lives rather than
    re-checked in the route."""


@dataclass(frozen=True)
class Cell:
    value: str | None
    differs: bool


@dataclass(frozen=True)
class Column:
    position: int
    role: str
    term: PolicyTerm
    admitted: str
    total: Decimal | None          # this column's own total premium
    total_delta: Decimal | None    # against the baseline; None on the baseline
    breakdown: PremiumBreakdown | None
    breakdown_reason: str | None


@dataclass(frozen=True)
class Row:
    difference: Difference
    baseline: Cell
    comparands: list[Cell]


@dataclass(frozen=True)
class Matrix:
    comparison: Comparison
    policy: Policy
    client: Client
    columns: list[Column]
    rows: list[Row]
    legacy: bool
    draft_eligible: bool


def _terms_for(session: Session, columns: list[ColumnSpec]) -> list[PolicyTerm]:
    if not columns:
        raise ColumnsRejected("a comparison needs at least one column")
    if len(columns) > MAX_COLUMNS:
        raise ColumnsRejected(
            f"a comparison holds at most {MAX_COLUMNS} columns: a baseline and "
            f"{MAX_COLUMNS - 1} comparands"
        )
    if [spec.role for spec in columns].count("baseline") != 1:
        raise ColumnsRejected("a comparison needs exactly one baseline column")
    if columns[0].role != "baseline":
        raise ColumnsRejected("the baseline is the first column")

    terms = [session.get(PolicyTerm, spec.policy_term_id) for spec in columns]
    missing = [
        spec.policy_term_id
        for spec, term in zip(columns, terms) if term is None
    ]
    if missing:
        raise ColumnsRejected(f"no such term: {missing}")
    if terms[0].kind != "bound":
        raise ColumnsRejected(
            "the baseline must be a bound term: measuring the incumbent "
            "against a quote inverts what every other column means"
        )
    if len({term.policy_id for term in terms}) != 1:
        raise ColumnsRejected(
            "every column must be a term of one policy: the policy chain is "
            "what makes these the same risk"
        )
    return terms


def _strongest(row: MatrixRow, rules: RuleSet) -> tuple[str, str]:
    """Each comparand that actually differs is classified against the
    baseline, and the strongest answer wins.

    Comparands equal to the baseline are skipped rather than classified. Most
    rules match on path alone — deductible_change has no `when` — so putting
    an unchanged pair to the rules would return material for a cell that did
    not move.

    Strongest wins because under-flagging is the dangerous direction: a row
    where one quote halves the liability limit is material even when every
    other column matches.
    """
    canonical = normalize(row.field_path, row.baseline_value)
    best: tuple[str, str] | None = None
    for value in row.comparand_values:
        if normalize(row.field_path, value) == canonical:
            continue
        result = classify(
            RawDifference(row.field_path, row.baseline_value, value), rules
        )
        if best is None or _STRENGTH[result[0]] < _STRENGTH[best[0]]:
            best = result
    assert best is not None, "diff_field_sets emits no row where nothing differs"
    return best


def build_matrix(
    session: Session,
    *,
    columns: list[ColumnSpec],
    rules: RuleSet,
    run_id: int | None = None,
    settings: Settings | None = None,
) -> Comparison:
    """The one way a comparison is written.

    settings is threaded from here rather than added later: Task 6 reads
    extras_config off it and Task 9 reads attention_premium_pct, and both are
    then internal changes rather than a signature every caller has to be
    re-edited for. None means neither of those behaviours runs, which is what
    the tests that construct a matrix without settings rely on.
    """
    terms = _terms_for(session, columns)

    comparison = Comparison(renewal_run_id=run_id)
    session.add(comparison)
    session.flush()

    column_rows = []
    for position, spec in enumerate(columns):
        row = ComparisonColumn(
            comparison_id=comparison.id, position=position,
            policy_term_id=spec.policy_term_id, role=spec.role,
        )
        session.add(row)
        column_rows.append(row)
    session.flush()

    field_sets = [FieldSet(term.id, term_field_map(session, term)) for term in terms]
    baseline_column, *comparand_columns = column_rows
    baseline_set, *comparand_sets = field_sets

    for row in diff_field_sets(baseline_set, comparand_sets):
        materiality, rule_id = _strongest(row, rules)
        difference = Difference(
            comparison_id=comparison.id, field_path=row.field_path,
            materiality=materiality, rule_id=rule_id,
        )
        session.add(difference)
        session.flush()
        session.add(DifferenceCell(
            difference_id=difference.id,
            comparison_column_id=baseline_column.id,
            value=row.baseline_value,
        ))
        for column, value in zip(comparand_columns, row.comparand_values):
            session.add(DifferenceCell(
                difference_id=difference.id, comparison_column_id=column.id,
                value=value,
            ))

    session.flush()
    session.refresh(comparison)
    return comparison


def build_comparison(
    session: Session,
    *,
    run_id: int,
    prior_term: PolicyTerm,
    renewal_term: PolicyTerm,
    rules: RuleSet,
) -> Comparison:
    """The pairwise case, kept so the promote path reads as it did."""
    return build_matrix(
        session,
        columns=[
            ColumnSpec(prior_term.id, "baseline"),
            ColumnSpec(renewal_term.id, "comparand"),
        ],
        rules=rules,
        run_id=run_id,
    )


def _total(field_map: dict[str, str | None]) -> Decimal | None:
    """normalize() already canonicalizes total_premium as money, so this needs
    no second money parser and premium.py stays untouched."""
    canonical = normalize("policy.total_premium", field_map.get("policy.total_premium"))
    if canonical is None:
        return None
    try:
        return Decimal(canonical)
    except InvalidOperation:
        return None


def _column(
    session: Session, position: int, role: str, term: PolicyTerm,
    policy: Policy, field_map: dict, baseline_term: PolicyTerm, baseline_map: dict,
) -> Column:
    carrier = resolve_carrier(session, term.carrier_name or "")
    admitted = (
        admitted_status(session, carrier.id, policy.state) if carrier else "unknown"
    )
    total = _total(field_map)

    if role == "baseline":
        # Its own total is carried even though it has no delta: the attention
        # rule needs it as the denominator and the prep sheet prints it as the
        # "was" figure.
        return Column(position, role, term, admitted, total, None, None, None)

    baseline_total = _total(baseline_map)
    delta = (
        (total - baseline_total).quantize(Decimal("0.01"))
        if baseline_total is not None and total is not None else None
    )
    # kind, not policy_id: a quote hangs off the incumbent's chain on purpose,
    # so policy_id is equal by construction and cannot discriminate one.
    if "quoted" in (term.kind, baseline_term.kind):
        return Column(
            position, role, term, admitted, total, delta, None, _ACROSS_CARRIERS
        )
    return Column(
        position, role, term, admitted, total, delta,
        attribute_premium(baseline_map, field_map), None,
    )


def matrix_for(
    session: Session, comparison: Comparison, *, include_noise: bool = True,
    settings: Settings | None = None,
) -> Matrix:
    """The one shape every consumer reads.

    A comparison with no comparison_column rows was built before the matrix.
    Its two columns and two cells are synthesized here from the pairwise
    columns, which is the only place in the codebase that knows they exist.
    """
    column_rows = (
        session.query(ComparisonColumn)
        .filter_by(comparison_id=comparison.id)
        .order_by(ComparisonColumn.position)
        .all()
    )
    legacy = not column_rows
    if legacy:
        terms = [
            session.get(PolicyTerm, comparison.prior_term_id),
            session.get(PolicyTerm, comparison.renewal_term_id),
        ]
        roles = ["baseline", "comparand"]
    else:
        terms = [session.get(PolicyTerm, row.policy_term_id) for row in column_rows]
        roles = [row.role for row in column_rows]

    policy = session.get(Policy, terms[0].policy_id)
    client = session.get(Client, policy.client_id)
    field_maps = [term_field_map(session, term) for term in terms]

    columns = [
        _column(session, position, role, term, policy, field_map,
                terms[0], field_maps[0])
        for position, (role, term, field_map)
        in enumerate(zip(roles, terms, field_maps))
    ]

    query = session.query(Difference).filter_by(comparison_id=comparison.id)
    if not include_noise:
        query = query.filter(Difference.materiality != "noise")
    differences = query.all()

    if legacy:
        values = {
            difference.id: (difference.prior_value, [difference.renewal_value])
            for difference in differences
        }
    else:
        by_column = {row.id: index for index, row in enumerate(column_rows)}
        cells = (
            session.query(DifferenceCell)
            .filter(DifferenceCell.difference_id.in_(
                [difference.id for difference in differences]
            ))
            .all()
        )
        collected: dict[int, list[str | None]] = {
            difference.id: [None] * len(column_rows) for difference in differences
        }
        for cell in cells:
            collected[cell.difference_id][by_column[cell.comparison_column_id]] = (
                cell.value
            )
        values = {
            difference_id: (row[0], row[1:])
            for difference_id, row in collected.items()
        }

    rows = []
    for difference in differences:
        baseline_value, comparand_values = values[difference.id]
        canonical = normalize(difference.field_path, baseline_value)
        rows.append(Row(
            difference=difference,
            baseline=Cell(baseline_value, False),
            comparands=[
                Cell(value,
                     normalize(difference.field_path, value) != canonical)
                for value in comparand_values
            ],
        ))
    rows.sort(key=lambda row: (
        _STRENGTH[row.difference.materiality], row.difference.field_path
    ))

    return Matrix(
        comparison=comparison,
        policy=policy,
        client=client,
        columns=columns,
        rows=rows,
        legacy=legacy,
        # Two bound terms of one policy is a renewal, and a renewal is the only
        # thing a client-facing draft can explain without recommending.
        draft_eligible=(
            len(terms) == 2
            and all(term.kind == "bound" for term in terms)
            and terms[0].policy_id == terms[1].policy_id
        ),
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

`breakdown_for` is deleted. Its two callers read `matrix.columns[1].breakdown` instead.

**Correction found while executing.** As first written, this task and Task 4 did not separate at this seam: Task 3 deletes `breakdown_for` and stops writing `difference.prior_value`/`renewal_value`, but the comparison route imports the first and the template reads the second — so the suite could not be green at the end of Task 3 as the task promised. The minimum needed to keep it green moved here, and it is genuinely minimal, because the screen is still two fixed columns headed Prior and Renewal:

- `renewal/web/comparison.py` reads `matrix_for` for the policy, the client and the breakdown, and hands the template a `cells` mapping of `difference_id -> (baseline_value, comparand_value)`.
- `comparison.html` reads that mapping instead of the two columns. Nothing else in it changes.
- `_MATERIALITY_ORDER` and the `Difference` query are deleted: `matrix_for` orders and filters, so `include_noise=bool(show_noise)` replaces them.

Task 4 still does the real work — N columns in the header, N value cells, per-comparand premium panels, the run crumb, the draft empty state and the CSS — and deletes the `cells` mapping when the template goes N-column.

`_strongest` hands `classify` a `RawDifference` — the diff's own dataclass, imported from `renewal.diff`, exactly the type it has always taken. `classify` learns nothing about matrices and `renewal/materiality.py` is not edited.

- [x] **Step 4: Move the draft onto cells**

In `renewal/draft.py`, `build_prompt` and `generate_draft` take the matrix. The system prompt, the instructions, and the 200-word rule do **not** change — only where the two values come from.

```python
def build_prompt(matrix) -> str:
    """Material and informational differences only. Noise never reaches the
    model.

    Two columns by construction: generate_draft is only called for a
    draft-eligible matrix, which is two bound terms of one policy.
    """
    lines = ["Changes at renewal:"]
    for row in matrix.rows:
        if row.difference.materiality == "noise":
            continue
        before = row.baseline.value if row.baseline.value is not None else "(absent)"
        after = (
            row.comparands[0].value
            if row.comparands[0].value is not None else "(absent)"
        )
        lines.append(
            f"- [{row.difference.materiality}] {row.difference.field_path}: "
            f"{before} -> {after}"
        )

    breakdown = matrix.columns[1].breakdown
    lines.append("")
    if breakdown is None or not breakdown.available:
        reason = breakdown.reason if breakdown else matrix.columns[1].breakdown_reason
        lines.append(
            f"Premium change cannot be broken down: {reason}. Say this "
            "plainly rather than speculating."
        )
        lines.append("")
        lines.append(INSTRUCTIONS)
        return "\n".join(lines)

    lines.append(f"Total premium change: {breakdown.total_delta}")
    for attribution in breakdown.lines:
        lines.append(f"  {attribution.amount} from {attribution.label}")
    lines.append(
        f"  {breakdown.residual} is not attributable from these documents. Say so; "
        "do not guess at a cause such as a rate increase."
    )
    lines.append("")
    lines.append(INSTRUCTIONS)
    return "\n".join(lines)


def generate_draft(session, matrix, *, client, settings) -> Draft:
    text = client.complete(
        model=settings.draft_model,
        system=SYSTEM,
        content=[text_block(build_prompt(matrix))],
    )
    draft = Draft(comparison_id=matrix.comparison.id, generated_text=text)
    session.add(draft)
    session.flush()
    return draft
```

- [x] **Step 5: Rewire the promote path**

In `renewal/web/review.py`, replace the `breakdown_for` import with `matrix_for`, and the tail of `promote_run`:

```python
            comparison = build_comparison(
                session,
                run_id=run.id,
                prior_term=terms[0],
                renewal_term=terms[1],
                rules=load_rules(settings.materiality_config),
            )
            matrix = matrix_for(session, comparison)
            if matrix.draft_eligible:
                generate_draft(
                    session, matrix, client=model_client, settings=settings
                )
            session.commit()
            comparison_id = comparison.id
```

The `Difference` import and the `session.query(Difference)` that fed the old call both go: the matrix carries the rows.

- [x] **Step 6: Rewrite the two tests that assert the old guarantees**

These are deliberate. A changed test is a changed guarantee, so each is rewritten rather than adjusted until it passes.

`tests/test_comparison.py`, the three assertions at 114-116:

```python
    # The term ids live on the columns now. A second copy on the comparison
    # could disagree with them, so it is not written.
    columns = (
        session.query(ComparisonColumn)
        .filter_by(comparison_id=comparison.id)
        .order_by(ComparisonColumn.position)
        .all()
    )
    assert [c.policy_term_id for c in columns] == [prior.id, renewal.id]
    assert [c.role for c in columns] == ["baseline", "comparand"]
    assert comparison.renewal_run_id == run.id
```

`tests/test_draft.py`, the fixture at 81-102: build the comparison through
`build_matrix` and read the prompt through `matrix_for`, rather than
hand-constructing a `Comparison` and two `Difference` rows. The assertions
about the prompt text itself do not change, because the prompt does not
change.

- [x] **Step 7: Run everything**

```bash
.venv/bin/pytest tests/test_matrix.py tests/test_comparison.py \
                 tests/test_draft.py tests/test_diff.py -v
.venv/bin/pytest
```
Expected: PASS throughout. `tests/test_diff.py` is still unedited.

- [x] **Step 8: Commit**

```bash
git add renewal/comparison.py renewal/draft.py renewal/web/review.py \
        tests/test_matrix.py tests/test_comparison.py tests/test_draft.py
git commit -m "feat(comparison): one write path and one read path for N columns"
```

---
### Task 4: The comparison screen on the matrix — CHECKPOINT

**Files:**
- Modify: `renewal/web/comparison.py` — the show route reads `matrix_for`
- Modify: `renewal/templates/comparison.html` — N value columns, N premium panels
- Test: `tests/test_web_comparison.py` — extend, do not rewrite

**Interfaces:**
- Consumes: `matrix_for` (Task 3).
- Produces: nothing new. This step must change no visible output for a two-column comparison.

Everything before this task is a refactor. Everything after it is new behaviour. Splitting here means a regression is attributable to one side or the other.

- [x] **Step 1: Extend the web test**

Add to `tests/test_web_comparison.py`, keeping every existing test:

```python
def test_a_two_column_comparison_still_renders_prior_and_renewal(client_app):
    """The checkpoint. Nothing a reader sees may move in this task."""
    ...
    page = client.get(f"/comparisons/{comparison_id}").text
    assert "3900.00" in page and "4210.00" in page
    assert "premium_total_change" in page


def test_a_legacy_comparison_renders_and_links_back_to_its_run(client_app):
    """A comparison written before the matrix has a run; the crumb points at
    it. One written from the record does not, and must not render a link to
    run #None."""


def test_a_comparison_with_no_run_does_not_render_a_run_crumb(client_app):
    page = client.get(f"/comparisons/{comparison_id}").text
    assert "/runs/None/review" not in page
```

- [x] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_web_comparison.py -v`
Expected: FAIL on the run-crumb test — the template renders `/runs/None/review`.

- [x] **Step 3: Rewrite the show route**

In `renewal/web/comparison.py`, delete `_MATERIALITY_ORDER` and the `Difference`
query — `matrix_for` orders and filters. Delete the `breakdown_for` import.

```python
    @router.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
    def show_comparison(request: Request, comparison_id: int, show_noise: int = 0):
        with session_factory() as session:
            comparison = session.get(Comparison, comparison_id)
            if comparison is None:
                raise HTTPException(status_code=404, detail="no such comparison")
            matrix = matrix_for(
                session, comparison, include_noise=bool(show_noise)
            )

            # A reclassification is recorded, not applied: the rule still says
            # what the row is. The screen shows both so the disagreement is
            # visible instead of looking like a control that did nothing.
            disagreements = {
                row.difference_id: row
                for row in session.query(Reclassification)
                .filter(Reclassification.difference_id.in_(
                    [row.difference.id for row in matrix.rows]
                ))
                .order_by(Reclassification.id)
            }

            return TEMPLATES.TemplateResponse(
                request,
                "comparison.html",
                {
                    "matrix": matrix,
                    "comparison": comparison,
                    "policy": matrix.policy,
                    "client": matrix.client,
                    "disagreements": disagreements,
                    "draft": latest_draft(session, comparison_id),
                    "show_noise": bool(show_noise),
                },
            )
```

- [x] **Step 4: Rewrite the template**

`renewal/templates/comparison.html`. The crumb becomes conditional:

```jinja
{% block crumb %}
<span class="crumb">
  {% if comparison.renewal_run_id %}
  <a href="/runs/{{ comparison.renewal_run_id }}/review">back to run
    #{{ comparison.renewal_run_id }}</a>
  {% else %}
  <a href="/clients/{{ client.id }}">back to {{ client.display_name }}</a>
  {% endif %}
</span>
{% endblock %}
```

The premium panel loops the comparands rather than reading one breakdown:

```jinja
    <h2 style="margin-top: 28px;">Premium</h2>
    {% for column in matrix.columns if column.role == "comparand" %}
    {% if matrix.columns | length > 2 %}
    <h3>{{ column.term.carrier_name or "unknown carrier" }}</h3>
    {% endif %}
    {% if column.total_delta is not none %}
    <p class="total-delta {% if column.total_delta > 0 %}up{% endif %}">
      Total change {{ "+" if column.total_delta > 0 }}{{ column.total_delta }}
    </p>
    {% endif %}
    {% if column.breakdown and column.breakdown.available %}
    ... the existing lines and residual table, reading column.breakdown ...
    {% else %}
    <p class="notice">
      {{ column.breakdown_reason or column.breakdown.reason }}.
    </p>
    {% endif %}
    {% endfor %}
```

The diff table grows a value column per column, and the empty cell becomes
words:

```jinja
      <thead>
        <tr>
          <th>Field</th>
          {% for column in matrix.columns %}
          <th>{{ column.term.carrier_name or "unknown" }}
            {% if column.role == "baseline" %}<span class="baseline">baseline</span>{% endif %}
          </th>
          {% endfor %}
          <th>Class</th>
        </tr>
      </thead>
      <tbody>
        {% for row in matrix.rows %}
        <tr {% if row.difference.materiality == "material" %}class="attn"{% endif %}>
          <td class="path">{{ row.difference.field_path | wbr }}</td>
          {% for cell in [row.baseline] + row.comparands %}
          <td class="path {{ 'differs' if cell.differs else 'same' }}">
            {% if cell.value is not none %}{{ cell.value }}
            {% else %}<span class="muted">not on this document</span>{% endif %}
          </td>
          {% endfor %}
          ... the existing Class cell, reading row.difference ...
        </tr>
        {% endfor %}
      </tbody>
```

The draft panel's empty state distinguishes the two reasons there is no draft:

```jinja
    {% if draft %}
    ... the existing form ...
    {% elif not matrix.draft_eligible %}
    <div class="empty">
      <p>No draft is written for this comparison.</p>
      <p>A note that sets carriers side by side is a recommendation however it
        is worded, and recommending coverage is licensed activity. The
        differences are below; the call is yours.</p>
    </div>
    {% else %}
    ... the existing "no draft was generated" empty state ...
    {% endif %}
```

CSS for `.differs`, `.same`, and `.baseline` goes in `app.css`. A cell equal to
the baseline is de-emphasized so the eye lands on divergence.

- [x] **Step 5: Run the tests**

```bash
.venv/bin/pytest tests/test_web_comparison.py -v
.venv/bin/pytest
```
Expected: PASS.

- [x] **Step 6: Look at it**

Start the app, open a real two-column comparison, and confirm against a
screenshot or memory of the previous version that nothing moved. This is the
checkpoint; do not continue if anything reads differently.

- [x] **Step 7: Commit**

```bash
git add renewal/web/comparison.py renewal/templates/comparison.html \
        renewal/static/app.css tests/test_web_comparison.py
git commit -m "feat(web): render a comparison as a matrix of one or more columns"
```

**What the checkpoint found.** The rendered text of a promoted comparison was
diffed against the same page before the task. Two lines moved, both on
purpose:

- Total change left the breakdown table for a paragraph above it. Task 5's
  quoted columns have a delta and no attribution table to put it in.
- Column headers became the carrier name — which on a renewal is the same
  carrier in both columns, and was literally "unknown" twice for terms
  promoted from documents that carry no carrier name. Headers are now the
  carrier only where the carriers differ; a renewal still reads Prior and
  Renewal. `td.prior`/`td.renewal` became `td.same`/`td.differs`, which lands
  on the same two colours for a two-column renewal and means something for a
  third column.

`table.diffs` no longer sizes its value columns by `nth-child`. The template
divides a fixed 38% between them, so two columns still get the 19% each they
had.

---

### Task 5: Quoted terms, the column picker, and admitted status

**Files:**
- Modify: `renewal/promote.py` — `kind` parameter
- Modify: `renewal/clients/overview.py:64` — `_latest_term` filters `kind='bound'`
- Modify: `renewal/web/comparison.py` — `GET /policies/{id}/compare`, `POST /comparisons`
- Create: `renewal/templates/compare_new.html`
- Test: `tests/test_web_compare_picker.py`, and extend `tests/test_client_overview.py`

**Interfaces:**
- Consumes: `build_matrix`, `ColumnsRejected`, `MAX_COLUMNS` (Task 3).
- Produces:
  - `promote(..., kind: str = "bound")`
  - `GET /policies/{policy_id}/compare` — the picker, accepting `?baseline=&comparand=&comparand=`
  - `POST /comparisons` — form fields `policy_id`, `baseline`, `comparand` (repeated)

- [ ] **Step 1: Write the failing tests**

The one that matters most, in `tests/test_client_overview.py`:

```python
def test_a_quote_is_never_the_current_term(session):
    """_latest_term is the only query in the tree that means "the current
    term". A quote read as current would print a competitor's premium on the
    client page as what the client is paying."""
    policy = ...
    bound = _term(session, policy, premium="4210.00", kind="bound")
    _term(session, policy, premium="3880.00", kind="quoted",
          carrier="Carrier B")            # later id, would win without the filter
    row = overview(session, policy.client_id, agency_id=1).policies[0]
    assert row.total_premium == "4210.00"
```

And in `tests/test_web_compare_picker.py`: the picker lists bound and quoted
terms separately; a POST with a baseline and two comparands redirects to the
new comparison; each refusal from `ColumnsRejected` comes back as a 400 with
its reason on the page rather than a traceback.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_client_overview.py tests/test_web_compare_picker.py -v`
Expected: FAIL — the quote wins `_latest_term`, and `/policies/1/compare` is 404.

- [ ] **Step 3: Filter the one dangerous query**

`renewal/clients/overview.py`:

```python
def _latest_term(session: Session, policy_id: int) -> PolicyTerm | None:
    """kind='bound' is not optional. A quoted term hangs off the incumbent's
    chain, so without the filter a competitor's offer would render as what the
    client is currently paying."""
    return session.scalar(
        select(PolicyTerm)
        .where(PolicyTerm.policy_id == policy_id)
        .where(PolicyTerm.kind == "bound")
        .order_by(PolicyTerm.id.desc())
        .limit(1)
    )
```

- [ ] **Step 4: Let promotion write a quoted term**

`renewal/promote.py`: add `kind: str = "bound"` to `promote`'s keyword-only
arguments and pass it to the `PolicyTerm(...)` constructor. Nothing else in
the function changes — a quote is promoted through the same gate, the same
corrections, and the same frozen snapshot as a bound term.

- [ ] **Step 5: Add the picker**

In `renewal/web/comparison.py`. The module imports `APIRouter, Form,
HTTPException, Request` today and needs `Query` as well, for the repeated
`comparand` parameter on the GET:

```python
    @router.get("/policies/{policy_id}/compare", response_class=HTMLResponse)
    def new_comparison(
        request: Request, policy_id: int,
        baseline: int | None = None, comparand: list[int] = Query(default=[]),
        error: str | None = None,
    ):
        """Terms of one policy, bound and quoted listed apart. baseline and
        comparand preselect, which is how the renewal_received attention item
        arrives here with both terms already ticked."""
        ...

    @router.post("/comparisons")
    def create_comparison(
        policy_id: int = Form(...),
        baseline: int = Form(...),
        comparand: list[int] = Form(default=[]),
    ):
        specs = [ColumnSpec(baseline, "baseline")] + [
            ColumnSpec(term_id, "comparand") for term_id in comparand
        ]
        with session_factory() as session:
            try:
                comparison = build_matrix(
                    session, columns=specs,
                    rules=load_rules(settings.materiality_config),
                )
            except ColumnsRejected as rejected:
                raise HTTPException(status_code=400, detail=str(rejected))
            session.commit()
            comparison_id = comparison.id
        return RedirectResponse(f"/comparisons/{comparison_id}", status_code=303)
```

No draft is generated here. `POST /comparisons` can produce a matrix with a
quoted column, and that is not draft-eligible; the renewal path in
`review.py` is the one that drafts.

- [ ] **Step 6: The picker template**

`renewal/templates/compare_new.html`, in the existing style. One radio group
for the baseline over bound terms only, one checkbox group for comparands over
every term, quoted ones grouped under their carrier and visually distinct.
State the cap in words next to the checkboxes: *"a baseline and up to four
others"*. No sort by premium anywhere on the page.

- [ ] **Step 7: Admitted status in the column headers**

`matrix_for` already resolves it. Put it in `comparison.html`'s header cell,
always, with no toggle, rendered as the word:

```jinja
          <th>{{ column.term.carrier_name or "unknown" }}
            <span class="admitted admitted-{{ column.admitted }}">
              {{ column.admitted | replace("_", "-") }}</span>
            {% if column.term.kind == "quoted" %}<span class="quoted">quote</span>{% endif %}
          </th>
```

An unresolved carrier or a policy with no state reads `unknown` in words, never
blank — the rule `client.html` already states: *blank reads as "nothing to
worry about", and unknown is not that.*

- [ ] **Step 8: Run and commit**

```bash
.venv/bin/pytest
git add renewal/promote.py renewal/clients/overview.py \
        renewal/web/comparison.py renewal/templates/ renewal/static/app.css \
        tests/test_web_compare_picker.py tests/test_client_overview.py
git commit -m "feat(comparison): set a renewal against the quotes for the same risk"
```

---

### Task 6: The extras map

**Files:**
- Create: `renewal/extras.py`, `config/extras.yaml`
- Modify: `renewal/fieldpath.py` — `extras.<key>` production
- Modify: `renewal/diff.py` — `normalize(..., value_type=None)`, `diff_field_sets(..., types=None)`, `term_field_map` reads extras
- Modify: `renewal/promote.py` — writes `policy_term_extra`
- Modify: `renewal/comparison.py` — loads types from `settings.extras_config`; **no signature change**, because Task 3 already threaded `settings` through
- Modify: `renewal/config.py`, `.env.example` — `EXTRAS_CONFIG`
- Test: `tests/test_extras.py`

**Interfaces:**
- Consumes: `record_correction(kind="omission")`, which already exists and needs no change.
- Produces:
  - `renewal.extras.load_types(path) -> dict[str, str]`
  - `normalize(field_path, value, value_type=None)`
  - `PolicyTermExtra` rows written at promotion

The extractor is not touched. Extras arrive through the add-missing-field control on the review screen, which already writes an `omission` correction against any field path, and `effective_values` already folds corrections into what promotion reads.

- [ ] **Step 1: Write the failing test**

`tests/test_extras.py`, covering: the grammar accepts `extras.surcharge_total`
and rejects `extras.a.b`; an unlisted key is `text`; a money extra makes
`$1,200` and `1200.00` equal while a text extra keeps them different; an
`omission` correction on an extras path promotes into `policy_term_extra` and
comes back out of `term_field_map`; a rule naming `extras.*` classifies one.

```python
def test_an_unlisted_key_is_text(tmp_path):
    """text compares by exact string, which is the answer that cannot be wrong
    in an interesting way."""
    types = load_types(_write(tmp_path, {"extras.known": "money"}))
    assert types.get("extras.unknown", "text") == "text"


def test_a_money_extra_compares_as_money():
    assert normalize("extras.surcharge", "$1,200.00", "money") == \
           normalize("extras.surcharge", "1200", "money")


def test_a_text_extra_does_not():
    assert normalize("extras.note", "$1,200.00", "text") != \
           normalize("extras.note", "1200", "text")


def test_a_core_path_still_guesses_from_its_leaf():
    """MONEY_LEAVES is right for a closed vocabulary and wrong for an open
    one, which is why extras pass the type and core paths do not."""
    assert normalize("coverage.COMP.premium", "$1,200.00") == \
           normalize("coverage.COMP.premium", "1200")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_extras.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.extras'`

- [ ] **Step 3: The grammar**

`renewal/fieldpath.py`, one production added to `_PATTERNS`:

```python
    # Carrier-specific, one segment. The type is not in the path: it lives in
    # config/extras.yaml, because it belongs to the key rather than to any one
    # term. Never a promoted column on policy_term.
    re.compile(rf"^extras\.{_SEG}$"),
```

Nothing else in that module changes, and `renewal/extract/validate.py` is not
edited: adding a production lets a path past `_check`'s first line, and the
source-text gate below it is untouched.

- [ ] **Step 4: The loader and the config**

`renewal/extras.py`:

```python
"""Types for carrier-specific fields.

Human-set rather than extracted. A model guessing whether an unfamiliar field
is money or text is the confident wrongness this project avoids elsewhere:
"1,200" is a premium on one form and part of a policy number on another.
Admitted status and billing type are human-set for the same reason.

The type belongs to the key, not to the term, so it lives here in one copy
rather than in a column on every row that could disagree with it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

VALUE_TYPES = ("money", "date", "integer", "text")
DEFAULT_TYPE = "text"


def load_types(path: Path) -> dict[str, str]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    types = data.get("types") or {}
    for key, value_type in types.items():
        if value_type not in VALUE_TYPES:
            raise ValueError(f"{key}: unknown value type {value_type}")
    return types
```

`config/extras.yaml`:

```yaml
# Types for carrier-specific fields, human-set. A key that is not listed is
# text, which compares by exact string — the answer that cannot be wrong in an
# interesting way.
#
# Nothing infers a type from a value. "1,200" is a premium on one form and part
# of a policy number on another, and guessing would make abs_delta_gte fire on
# a string that is not a quantity.
version: 1
default: text
types: {}
```

- [ ] **Step 5: Typed normalization**

`renewal/diff.py`:

```python
def normalize(
    field_path: str, value: str | None, value_type: str | None = None
) -> str | None:
    """Canonicalize type only. Never suppresses a difference.

    value_type is passed for extras, whose vocabulary is open. Core paths
    leave it None and keep guessing from the leaf name, which is correct for a
    closed vocabulary.
    """
    if value is None:
        return None
    text = " ".join(value.split())
    money = (
        value_type == "money" if value_type is not None
        else field_path.split(".")[-1] in MONEY_LEAVES
    )
    if money:
        try:
            return str(Decimal(re.sub(r"[$,\s]", "", text)).normalize())
        except InvalidOperation:
            return text
    return text
```

`diff_field_sets` gains `types: dict[str, str] | None = None` and resolves per
path before comparing. `term_field_map` reads `policy_term_extra` into the flat
map alongside everything else, so the diff, the rules and the screen need no
knowledge that a path is an extra.

`build_matrix` and `matrix_for` load the types themselves from
`settings.extras_config` and pass them to `diff_field_sets`, `_strongest`, and
the `differs` computation. **Neither signature changes and no caller is
touched**: Task 3 threaded `settings` through for exactly this. When `settings`
is None the types map is empty and every extra compares as text, which is the
safe direction.

- [ ] **Step 6: Promotion writes them**

`renewal/promote.py`, after the coverage and item loops:

```python
    for path, value in sorted(values.items()):
        if path.startswith("extras."):
            session.add(PolicyTermExtra(
                policy_term_id=term.id, field_path=path, value=value
            ))
```

- [ ] **Step 7: Settings**

`EXTRAS_CONFIG`, defaulted to `config/extras.yaml`, exactly mirroring
`MATERIALITY_CONFIG` in `renewal/config.py` and `.env.example`.

- [ ] **Step 8: Run and commit**

```bash
.venv/bin/pytest
git add renewal/extras.py renewal/fieldpath.py renewal/diff.py \
        renewal/promote.py renewal/comparison.py renewal/config.py \
        config/extras.yaml .env.example tests/test_extras.py
git commit -m "feat(diff): compare carrier-specific fields without promoting a column"
```

---
### Task 7: Wire stage 7, and route quotes through it

**Files:**
- Modify: `renewal/classify/runner.py:30` — `FIELD_EXTRACTION_CLASSES`
- Modify: `renewal/pipeline.py` — `run_fields_stage`, called from `ingest_document`
- Modify: `scripts/bulk_import.py` — `--skip-fields`, default on for bulk import
- Modify: `README.md` — which paths extract, and what that costs
- Test: `tests/test_pipeline.py` — extend

**Interfaces:**
- Consumes: `should_extract_fields`, which has existed since Phase 2 and which nothing calls.
- Produces: `run_fields_stage(session, store, document, *, client, settings)`.

`should_extract_fields` is a routing predicate with no caller. `ingest_document` runs text, resolve, dates, classify and attention, then stops, so structured extraction only ever happens on the two-upload path in `renewal/web/runs.py`. A quote that is stored, classified and searchable but never extracted cannot become a column.

**This is the first change that spends extraction-model calls during bulk import.** An archive of ten thousand mostly-declarations PDFs will now cost real money against a hosted provider. That is why the flag exists and why it defaults the way it does.

- [ ] **Step 1: Write the failing test**

Extend `tests/test_pipeline.py`:

```python
def test_a_quote_routes_to_field_extraction(session):
    """Classified and stored was enough in Phase 2. A quote that is never
    extracted cannot become a comparison column."""
    assert should_extract_fields(session, _classified(session, "quote").id)


def test_an_invoice_still_does_not(session):
    assert not should_extract_fields(session, _classified(session, "invoice").id)


def test_ingest_runs_the_field_stage_for_a_declarations_document(session):
    calls = []
    ingest_document(..., model_client=_recording(calls), settings=settings)
    assert any(call == "extract" for call in calls)


def test_ingest_skips_the_field_stage_when_it_is_switched_off(session):
    """Import is for getting documents in. Extraction is a pure function of
    (blob, extractor_version) and can be re-run at any time."""
    calls = []
    ingest_document(..., extract_fields=False, model_client=_recording(calls),
                    settings=settings)
    assert "extract" not in calls


def test_a_failed_field_stage_does_not_lose_the_document(session):
    """Every stage after storage is best-effort. A provider outage must not
    cost the import."""
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_pipeline.py -v`
Expected: FAIL — `quote` is not in `FIELD_EXTRACTION_CLASSES`, and no extraction is attempted.

- [ ] **Step 3: Route quotes**

`renewal/classify/runner.py`:

```python
# Only these route on to structured field extraction. quote joined them when
# the comparison engine learned to set a quote beside a renewal; before that it
# was classified and stored and nothing consumed it.
FIELD_EXTRACTION_CLASSES = ("declarations", "endorsement", "quote")
```

- [ ] **Step 4: Wire the stage**

`renewal/pipeline.py`, following the shape of every other stage — best-effort,
logged, separately re-runnable. It needs one new import,
`from renewal.extract.runner import extract`; `should_extract_fields` is
already defined in the module and finally gets its caller:

```python
def run_fields_stage(
    session: Session, store: BlobStore, document: Document, *,
    client: ModelClient, settings: Settings,
) -> None:
    if not should_extract_fields(session, document.id):
        return
    try:
        extract(session, store, document, "v1", client=client, settings=settings)
    except Exception:  # noqa: BLE001 - the document survives a failed stage
        logger.exception("fields stage failed document_id=%s", document.id)
```

and in `ingest_document`, after classify and before attention, because the
attention rules read what the stages above wrote:

```python
    # Costs a model call per routed document, which is why the caller can turn
    # it off. Bulk import does; manual upload and email intake do not.
    if extract_fields and model_client is not None and settings is not None:
        run_fields_stage(
            session, store, document, client=model_client, settings=settings
        )
```

`ingest_document` gains `extract_fields: bool = True`. Defaulting to true keeps
the interactive paths whole; the one caller that turns it off says so.

- [ ] **Step 5: The flag**

`scripts/bulk_import.py`:

```python
    parser.add_argument(
        "--skip-fields", action="store_true", default=True,
        help="do not run structured field extraction (default). Import is for "
             "getting documents in; extraction is a pure function of the blob "
             "and can be re-run at any time.",
    )
    parser.add_argument(
        "--extract-fields", dest="skip_fields", action="store_false",
        help="run structured field extraction during the import. Costs one "
             "model call per declarations, endorsement or quote document.",
    )
```

and pass `extract_fields=not args.skip_fields` to `ingest_document`.

- [ ] **Step 6: Say what it costs**

In `README.md`, under the bulk-import section, next to where it already says
extraction is re-runnable:

```markdown
Bulk import does not run structured field extraction. Declarations,
endorsements and quotes are stored, text-extracted, dated, classified and
matched — all of which is local or cheap — but reading the coverage grid out
of them is a model call each, and an archive is thousands of documents. Pass
`--extract-fields` to do it during the import, or leave it and re-run
extraction later against whatever subset is worth it: extraction is a pure
function of (blob, extractor_version) and can be re-run at any time.

Manual upload and email intake do extract, because they are one document at a
time and the result is wanted immediately.
```

- [ ] **Step 7: Run and commit**

```bash
.venv/bin/pytest
git add renewal/classify/runner.py renewal/pipeline.py scripts/bulk_import.py \
        README.md tests/test_pipeline.py
git commit -m "feat(pipeline): extract fields from what arrives through the pipe"
```

---

### Task 8: Promotion in the pipeline

**Files:**
- Modify: `renewal/pipeline.py` — `run_promote_stage`
- Test: `tests/test_pipeline_promote.py`

**Interfaces:**
- Consumes: `promote`, `unresolved_field_paths` (`renewal/promote.py`, unedited except for Task 5's `kind`); `latest_link` (`renewal/resolve/service.py`).
- Produces: `run_promote_stage(session, document, *, settings) -> PolicyTerm | None`.

Without this, D10 delivers nothing: `renewal_received` is defined at promotion, and nothing that arrives through the pipe is ever promoted, so the item would only appear for renewals already walked through the review screen by hand — where the comparison is one click away regardless.

The gate reads two already-recorded facts and adds no new judgment.

- [ ] **Step 1: Write the failing test**

`tests/test_pipeline_promote.py`:

```python
"""Promotion as a pipeline stage.

The gate is two facts that are already recorded: the document is linked to a
policy, and the extraction has nothing flagged. Neither is inferred here, and
promote() itself is unchanged — it still raises rather than guessing.
"""


def test_a_clean_extraction_on_a_linked_policy_promotes(session):
    term = run_promote_stage(session, document, settings=settings)
    assert term is not None
    assert term.policy_id == policy.id
    assert term.kind == "bound"


def test_a_flagged_field_waits_for_a_human(session):
    """promote() would raise PromotionBlocked. The stage does not catch that
    and carry on — it declines to call it."""
    assert run_promote_stage(session, document, settings=settings) is None


def test_a_document_linked_to_a_client_but_no_policy_does_not_promote(session):
    """PolicyTerm.policy_id is not nullable, and guessing which of a client's
    four policies a dec page belongs to is the misfiling D8 exists to
    prevent."""
    assert run_promote_stage(session, document, settings=settings) is None


def test_an_unlinked_document_does_not_promote(session):
    assert run_promote_stage(session, document, settings=settings) is None


def test_a_quote_promotes_as_a_quoted_term(session):
    term = run_promote_stage(session, quote_document, settings=settings)
    assert term.kind == "quoted"


def test_promoting_twice_writes_one_term(session):
    """Idempotent like every other stage: a document already promoted at this
    extraction is skipped, not promoted again."""
    run_promote_stage(session, document, settings=settings)
    assert run_promote_stage(session, document, settings=settings) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_pipeline_promote.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_promote_stage'`

- [ ] **Step 3: Write the stage**

New imports in `renewal/pipeline.py`: `PromotionBlocked`, `promote` and
`unresolved_field_paths` from `renewal.promote`, plus `Extraction` and
`PolicyTerm` on the existing `renewal.models` import. `latest_link` and
`latest_class` are already imported.

```python
def run_promote_stage(
    session: Session, document: Document, *, settings: Settings
) -> PolicyTerm | None:
    """Promote what needs no human, and leave everything else alone.

    Phase 2's pipeline diagram specified this stage — "promotion to PolicyTerm,
    only above confidence threshold" — and it was never built, so nothing that
    arrived through the pipe ever became a term.

    Two conditions, both already-recorded facts:

      - the latest document_link carries a policy_id, which under D8 means an
        exact policy-number match, the only auto-link this system performs; and
      - the extraction has no needs_review fields, so promote() would not raise.

    Nothing new is inferred and no threshold is softened. An extraction failing
    either condition waits for a human exactly as it does today.
    """
    link = latest_link(session, document.id)
    if link is None or link.policy_id is None:
        return None

    extraction = (
        session.query(Extraction)
        .filter_by(document_id=document.id)
        .order_by(Extraction.id.desc())
        .first()
    )
    if extraction is None:
        return None
    if unresolved_field_paths(session, extraction.id):
        return None
    if session.query(PolicyTerm).filter_by(
        promoted_from_extraction_id=extraction.id
    ).first():
        return None  # idempotent, like every other stage

    kind = "quoted" if latest_class(session, document.id) == "quote" else "bound"
    try:
        return promote(session, extraction, link.policy_id, kind=kind)
    except PromotionBlocked:
        # Unreachable given the check above, and caught anyway: a malformed
        # date is blocked by promote() for a reason the stage cannot see.
        logger.info("promotion blocked document_id=%s", document.id)
        return None
```

Called from `ingest_document` after the fields stage and before attention, so
the attention rules in Task 9 can read the term it wrote.

- [ ] **Step 4: Run and commit**

```bash
.venv/bin/pytest
git add renewal/pipeline.py tests/test_pipeline_promote.py
git commit -m "feat(pipeline): promote what arrives clean and linked to a policy"
```

---

### Task 9: `renewal_received` and `premium_change`

**Files:**
- Modify: `renewal/attention/rules.py` — `evaluate_promotion`, `evaluate_comparison`, and the stale comment
- Modify: `renewal/pipeline.py` — call `evaluate_promotion`
- Modify: `renewal/web/review.py` — call `evaluate_promotion`
- Modify: `renewal/comparison.py` — `build_matrix` calls `evaluate_comparison`
- Modify: `renewal/config.py`, `.env.example` — `ATTENTION_PREMIUM_PCT`
- Modify: `renewal/templates/attention.html` — the compare link
- Test: `tests/test_attention.py` — extend

**Interfaces:**
- Consumes: `_add`, `_has_item` (existing, idempotent).
- Produces:
  - `evaluate_promotion(session, term) -> AttentionItem | None`
  - `evaluate_comparison(session, comparison, matrix, *, settings) -> AttentionItem | None`

`REASONS` does not change. Both codes were declared in Phase 2 so the vocabulary would be stable across exactly this change.

- [ ] **Step 1: Write the failing test**

Extend `tests/test_attention.py`:

```python
def test_a_renewal_term_on_an_existing_policy_is_flagged(session):
    """A fact about two rows, not a judgment about a PDF: a bound term on a
    policy that already holds a bound term with an earlier effective date."""


def test_the_first_term_on_a_policy_is_not_a_renewal(session): ...


def test_a_quoted_term_is_not_a_renewal(session): ...


def test_a_backdated_term_is_not_a_renewal(session):
    """A term whose effective date precedes the existing one is a correction
    to history, not a renewal."""


def test_premium_change_fires_above_the_threshold(session): ...


def test_premium_change_never_fires_on_a_quoted_column(session):
    """Cross-carrier, the delta is real but it is not a change to anything —
    it is two carriers pricing the same risk differently."""


def test_neither_rule_duplicates_on_a_second_run(session): ...
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_attention.py -v`
Expected: FAIL — `ImportError: cannot import name 'evaluate_promotion'`

- [ ] **Step 3: Replace the stale comment and write the rules**

The comment at `renewal/attention/rules.py:44-49` becomes accurate:

```python
# renewal_received and premium_change are written by evaluate_promotion and
# evaluate_comparison rather than by evaluate(), because neither is a fact
# about a document at ingest. The question "is this dec page a renewal or a
# new-business bind?" needed domain knowledge nobody had; the question "is
# this term a renewal of that one?" is arithmetic over two rows.
```

`renewal/attention/rules.py` needs `Decimal` and `PolicyTerm` added to its
imports; `select` and `Session` are already there.

```python
def evaluate_promotion(session: Session, term: PolicyTerm) -> AttentionItem | None:
    """A bound term on a policy that already holds a bound term with an
    earlier effective date.

    Nothing auto-builds the comparison. The item carries a link to the picker
    with both terms preselected and she clicks it.
    """
    if term.kind != "bound" or term.effective_date is None:
        return None
    earlier = session.scalar(
        select(PolicyTerm.id)
        .where(PolicyTerm.policy_id == term.policy_id)
        .where(PolicyTerm.kind == "bound")
        .where(PolicyTerm.id != term.id)
        .where(PolicyTerm.effective_date < term.effective_date)
        .limit(1)
    )
    if earlier is None or term.source_document_id is None:
        return None
    return _add(
        session, term.source_document_id, "renewal_received",
        "Renewal received. Compare it with the prior term?",
    )


def evaluate_comparison(
    session: Session, *, document_id: int | None, baseline_total: Decimal | None,
    total_delta: Decimal | None, settings,
) -> AttentionItem | None:
    """Renewal comparisons only. Across carriers the delta is two carriers
    pricing the same risk differently, not a change to anything.

    Plain values rather than a Matrix on purpose. This module is imported by
    renewal/comparison.py, so taking its dataclass — or calling matrix_for to
    get one — would close an import cycle. The caller already holds every
    number this needs.
    """
    if document_id is None or total_delta is None or not baseline_total:
        return None
    pct = abs(total_delta / baseline_total) * 100
    if pct < settings.attention_premium_pct:
        return None
    return _add(
        session, document_id, "premium_change",
        f"Premium moved {total_delta:+} ({pct:.0f}%) at renewal",
    )
```

- [ ] **Step 4: Call them**

`renewal/pipeline.py` — after `run_promote_stage` returns a term.
`renewal/web/review.py` — after each `promote()` in the promote loop.
`renewal/comparison.py` — at the end of `build_matrix`, which already holds
the settings keyword from Task 3. It reads its own `matrix_for` to get the
numbers and passes **plain values** to `evaluate_comparison`, never the
`Matrix`: `renewal/comparison.py` imports `renewal/attention/rules.py`, so
handing the dataclass across would close an import cycle.

```python
    if settings is not None:
        matrix = matrix_for(session, comparison, settings=settings)
        if matrix.draft_eligible:
            evaluate_comparison(
                session,
                document_id=matrix.columns[1].term.source_document_id,
                baseline_total=matrix.columns[0].total,
                total_delta=matrix.columns[1].total_delta,
                settings=settings,
            )
```

`settings=None` skips the rule, which is what the tests that build a matrix
without settings rely on.

- [ ] **Step 5: The link**

`renewal/templates/attention.html`: a `renewal_received` row gets a
**Compare** button pointing at
`/policies/{{ policy_id }}/compare?baseline={{ prior }}&comparand={{ current }}`.
That is the one click D10 describes; nothing builds until she takes it.

- [ ] **Step 6: Settings, run, commit**

`ATTENTION_PREMIUM_PCT`, default 10, in `renewal/config.py` and `.env.example`.

```bash
.venv/bin/pytest
git add renewal/attention/rules.py renewal/pipeline.py renewal/web/review.py \
        renewal/comparison.py renewal/config.py .env.example \
        renewal/templates/attention.html tests/test_attention.py
git commit -m "feat(attention): flag a renewal that arrived and a premium that moved"
```

---

### Task 10: The call prep sheet

**Files:**
- Create: `renewal/clients/prep.py`, `renewal/templates/prep.html`
- Modify: `renewal/web/clients.py` — `GET /clients/{id}/prep`
- Modify: `renewal/static/app.css` — the first `@media print` block
- Modify: `renewal/templates/client.html` — a link to it
- Test: `tests/test_prep.py`, `tests/test_web_prep.py`

**Interfaces:**
- Consumes: `overview()` (`renewal/clients/overview.py`, unedited beyond Task 5), `matrix_for`.
- Produces: `prep(session, client_id, *, agency_id, today=None) -> CallPrep`.

**It computes nothing and calls no model.** Every number on it already exists in a row that something else wrote. A generated paragraph she reads to a client over the phone is the one place in this system where a hallucination reaches a client with no document, no draft and no second look in between.

- [ ] **Step 1: Write the failing test**

`tests/test_prep.py`:

```python
def test_the_sheet_makes_no_model_call(session):
    """Asserted with a client stub that raises. The sheet is an assembly of
    stored facts; a model would only restate them less reliably."""
    class Exploding:
        def complete(self, **kwargs):
            raise AssertionError("the prep sheet must not call a model")

    prep(session, client_id, agency_id=1)   # no client passed at all


def test_a_policy_renewing_inside_the_window_carries_its_comparison(session):
    row = prep(session, client_id, agency_id=1).renewals[0]
    assert row.days_to_renewal == 19
    assert row.total_delta == Decimal("310.00")
    assert "COMP" in row.material_changes[0]


def test_a_policy_with_no_comparison_says_so(session):
    """Rather than comparing on the spot, which would be a model call inside a
    page load she opened while the phone was ringing."""
    row = prep(session, client_id, agency_id=1).renewals[0]
    assert row.comparison_id is None


def test_an_unconfirmed_date_says_it_is_unconfirmed(session): ...


def test_a_derived_date_shows_its_arithmetic(session): ...


def test_unknown_renders_as_the_word(session): ...


def test_the_residual_is_stated_as_unattributable(session):
    """Never as a cause. A dec page shows the what, not the why."""
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_prep.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.clients.prep'`

- [ ] **Step 3: The assembler**

`renewal/clients/prep.py` — a second view over `overview()`, not a second
assembly. `CallPrep` carries the client, the renewals inside
`RENEWAL_WINDOW_DAYS`, upcoming dates with their confirmation state, open
attention items, and recent messages. Each renewal row reads the latest
comparison for its policy through `matrix_for` and pulls the total delta, the
material rows, and the residual out of it; a policy with no comparison carries
`comparison_id=None` and the page offers the picker.

Rules it follows, each inherited rather than invented: an unconfirmed date says
so everywhere; a derived date shows its arithmetic and is marked computed;
`unknown` is printed as the word; the residual is stated as unattributable and
never as a cause; nothing on the sheet is a recommendation.

- [ ] **Step 4: The template and the print stylesheet**

`renewal/templates/prep.html`, dense, in the existing style, matching the
layout in the spec. Then the first `@media print` block in `app.css`:

```css
/* The prep sheet is read on paper as often as on screen. Nothing that cannot
   be clicked on paper is printed. */
@media print {
  .topbar, .crumb, .diff-toolbar a, form, .btn { display: none !important; }
  body { background: #fff; color: #000; }
  .panel { border: none; box-shadow: none; break-inside: avoid; }
  a[href]::after { content: ""; }
}
```

- [ ] **Step 5: Run and commit**

```bash
.venv/bin/pytest
git add renewal/clients/prep.py renewal/web/clients.py \
        renewal/templates/prep.html renewal/templates/client.html \
        renewal/static/app.css tests/test_prep.py tests/test_web_prep.py
git commit -m "feat(clients): a call prep sheet that computes nothing"
```

---

## Verification

After Task 10, all of this must hold:

- `.venv/bin/pytest` is green.
- `git grep -n "prior_value\|renewal_value" renewal/` shows them only in
  `models.py` and in `matrix_for`'s legacy branch. Nothing writes them.
- `git grep -n "_latest_term" renewal/` shows one definition, and it filters
  `kind = 'bound'`.
- `git diff --stat master -- renewal/premium.py renewal/materiality.py
  renewal/extract/` is empty.
- A comparison built before this branch still renders at `/comparisons/<id>`,
  with its run crumb, its draft, and its premium breakdown.
- A three-column comparison renders at 390px without horizontal scroll, and
  every column header shows admitted status in words.
- `POST /comparisons` with six columns returns a 400 naming the cap.
- Importing a directory prints no extraction calls; adding `--extract-fields`
  prints one per declarations, endorsement and quote document.
- Dropping a renewal dec page into the mail drop produces a `renewal_received`
  item whose Compare button lands on the picker with both terms ticked, and
  builds nothing until it is clicked.

## What this deliberately does not do

Recorded so a reviewer does not read these as omissions:

- **No coverage-code normalization across carriers.** Carrier A's `COMP` and
  Carrier B's `OTC` are two rows. Normalizing on one worked example would
  produce a mapping that is confidently wrong on the next carrier.
- **No check that the quotes are for the same risk.** A quote written for three
  vehicles against a renewal covering five shows as two dropped vehicles. The
  rows are all correct; reading them is her job.
- **No quote expiry.** Quotes go stale in days and nothing tracks it.
- **No single-document review screen.** The review screen is per-run and
  pairwise, so a piped document with a flagged field cannot be corrected or
  promoted; it fails the Task 8 gate and waits. Its dates still reach the
  calendar and its text is still searchable.
- **The extractor still cannot emit an extras field.** Extras arrive only by
  human correction. Teaching the extractor is a new prompt version, a
  re-extraction, and a new baseline per provider and model.
- **No ranking, no scoring, no recommendation**, and no sort by premium
  anywhere.
- **No attribution.** Nothing records who built a comparison, reclassified a
  row, or printed a sheet.
- **The index page still lists runs.** It has been the wrong front door since
  the record layer shipped, and a comparison built from the record does not
  appear on it at all.
