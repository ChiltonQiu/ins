# Record Layer, Plan B: Search and Client Overview — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One search box that finds any document in under 300ms, and one screen that holds everything about a client, readable while she is on the phone.

**Architecture:** Search is Postgres full-text over `document_text.tsv` unioned with trigram matches on client, policy, and carrier names. No vector database and no embeddings. The overview is a set of narrow queries against the record built in Plan A, assembled in one template with nothing hidden behind a click.

**Tech Stack:** PostgreSQL full-text search (`websearch_to_tsquery`, `ts_headline`, GIN) and `pg_trgm`, SQLAlchemy 2.0, FastAPI, Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-record-layer-design.md`

**Covers:** spec build-order steps 6–7. Requires Plan A complete and her archive imported.

## Global Constraints

- Every table is insert-only. No UPDATE, no DELETE in application code.
- Logging carries ids, hashes, counts, and statuses only. Never document content.
- Search must never depend on field-extraction accuracy or on classification being correct.
- Admitted status is never inferred from a document. Human-set only.
- `unknown` is an acceptable answer and is preferred to a guess.
- One concern per commit; a migration is never committed with a feature.
- Both `pg_trgm` and the `document_text` GIN index already exist from Plan A migrations. This plan adds indexes only, never tables.

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `renewal/search/__init__.py`, `renewal/search/query.py` | The one search query and its result shape |
| `renewal/web/search.py`, `renewal/templates/search.html` | Search box and results |
| `renewal/clients/__init__.py`, `renewal/clients/overview.py` | Everything about one client, assembled |
| `renewal/web/clients.py`, `renewal/templates/client.html` | The overview screen |
| `renewal/carriers.py` | Carrier name resolution and admitted-status lookup |

**Modified**

| Path | Change |
|---|---|
| `renewal/web/__init__.py` | Register the two new routers |
| `renewal/web/settings.py` | Carrier admitted status and policy billing type controls |
| `migrations/versions/` | One migration: search indexes only |

---

## Task 1: Carrier resolution and admitted status

The overview needs a carrier's admitted status for the policy's state, and `carrier_name` is free text on `Policy` and `PolicyTerm`. Resolution is exact-match only: a fuzzy carrier match that silently picked the wrong company would be invisible and wrong in the direction that matters.

**Files:**
- Create: `renewal/carriers.py`
- Test: `tests/test_carriers.py` (create)

**Interfaces:**
- Consumes: models `Carrier`, `CarrierAlias`, `CarrierAdmittedStatus`, `Policy`.
- Produces: `normalize_name(value: str) -> str`, `resolve_carrier(session, name: str) -> Carrier | None`, `admitted_status(session, carrier_id: int, state: str | None) -> str`, `unresolved_carrier_names(session) -> list[str]`, `add_alias(session, carrier_id: int, name: str) -> CarrierAlias`, `set_admitted(session, carrier_id: int, state: str, status: str, *, set_by: str = "human") -> CarrierAdmittedStatus`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_carriers.py
from renewal.carriers import (
    add_alias, admitted_status, resolve_carrier, set_admitted,
    unresolved_carrier_names,
)
from renewal.models import Carrier, Client, Policy


def _carrier(session, name="Progressive Casualty Ins Co"):
    carrier = Carrier(display_name=name)
    session.add(carrier)
    session.flush()
    return carrier


def _policy(session, carrier_name, state=None):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name=carrier_name,
                    policy_number="P-1", line_of_business="commercial_auto",
                    state=state)
    session.add(policy)
    session.flush()
    return policy


def test_an_exact_name_resolves(session):
    carrier = _carrier(session)
    assert resolve_carrier(session, "Progressive Casualty Ins Co").id == carrier.id


def test_case_and_spacing_do_not_matter(session):
    carrier = _carrier(session)
    assert resolve_carrier(session, "  progressive  casualty ins co ").id == carrier.id


def test_an_alias_resolves(session):
    carrier = _carrier(session)
    add_alias(session, carrier.id, "Progressive")
    assert resolve_carrier(session, "Progressive").id == carrier.id


def test_an_unknown_name_resolves_to_nothing_rather_than_a_guess(session):
    _carrier(session)
    assert resolve_carrier(session, "Some Other Insurance Company") is None


def test_admitted_status_is_read_for_the_policys_state(session):
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "non_admitted")
    set_admitted(session, carrier.id, "AZ", "admitted")
    assert admitted_status(session, carrier.id, "CA") == "non_admitted"
    assert admitted_status(session, carrier.id, "AZ") == "admitted"


def test_the_latest_setting_for_a_state_wins(session):
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "unknown")
    set_admitted(session, carrier.id, "CA", "admitted")
    assert admitted_status(session, carrier.id, "CA") == "admitted"


def test_a_state_with_no_setting_is_unknown(session):
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "admitted")
    assert admitted_status(session, carrier.id, "NV") == "unknown"


def test_a_policy_with_no_state_is_unknown_not_assumed(session):
    """A carrier admitted in one state says nothing about another."""
    carrier = _carrier(session)
    set_admitted(session, carrier.id, "CA", "admitted")
    assert admitted_status(session, carrier.id, None) == "unknown"


def test_unresolved_names_are_listed_for_her_to_alias(session):
    _carrier(session)
    _policy(session, "Some Other Insurance Company")
    assert "Some Other Insurance Company" in unresolved_carrier_names(session)


def test_a_resolved_name_is_not_listed(session):
    carrier = _carrier(session)
    _policy(session, "Progressive")
    add_alias(session, carrier.id, "Progressive")
    assert unresolved_carrier_names(session) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_carriers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.carriers'`

- [ ] **Step 3: Implement**

```python
# renewal/carriers.py
"""Resolving a free-text carrier name to a carrier, and reading its admitted
status.

Exact match only, on the normalized display name or a normalized alias. Fuzzy
carrier matching is deliberately absent: picking the wrong company would be
invisible, and the value it feeds — admitted versus non-admitted — changes how
a policy should be read.

Admitted status is per state because the same carrier can be admitted in one
and surplus-lines in another. A state with no recorded status is unknown, and
so is a policy with no state. Neither is inferred.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.models import Carrier, CarrierAdmittedStatus, CarrierAlias, Policy


def normalize_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def resolve_carrier(session: Session, name: str) -> Carrier | None:
    target = normalize_name(name)
    for carrier in session.scalars(select(Carrier)):
        if normalize_name(carrier.display_name) == target:
            return carrier
    alias = session.scalar(
        select(CarrierAlias).where(CarrierAlias.alias == target)
    )
    return session.get(Carrier, alias.carrier_id) if alias else None


def add_alias(session: Session, carrier_id: int, name: str) -> CarrierAlias:
    alias = CarrierAlias(carrier_id=carrier_id, alias=normalize_name(name))
    session.add(alias)
    session.flush()
    return alias


def set_admitted(
    session: Session, carrier_id: int, state: str, status: str,
    *, set_by: str = "human",
) -> CarrierAdmittedStatus:
    row = CarrierAdmittedStatus(
        carrier_id=carrier_id, state=state.upper(), status=status, set_by=set_by
    )
    session.add(row)
    session.flush()
    return row


def admitted_status(session: Session, carrier_id: int, state: str | None) -> str:
    if not state:
        return "unknown"
    return session.scalar(
        select(CarrierAdmittedStatus.status)
        .where(CarrierAdmittedStatus.carrier_id == carrier_id)
        .where(CarrierAdmittedStatus.state == state.upper())
        .order_by(CarrierAdmittedStatus.id.desc())
        .limit(1)
    ) or "unknown"


def unresolved_carrier_names(session: Session) -> list[str]:
    """Names on policies that resolve to no carrier. Surfaced so she can alias
    or create them, rather than created automatically."""
    names = session.scalars(select(Policy.carrier_name).distinct())
    return sorted(n for n in names if n and resolve_carrier(session, n) is None)
```

`resolve_carrier` iterating carriers in Python is fine at an agency's scale — a
book has tens of carriers, not thousands — and keeps normalization identical
between the display name and the alias. Revisit only if that stops being true.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_carriers.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/carriers.py tests/test_carriers.py
git commit -m "feat(carriers): exact-match resolution and per-state admitted status"
```

---

## Task 2: Carrier and billing controls on the settings page

**Files:**
- Modify: `renewal/web/settings.py`, `renewal/templates/settings.html`
- Test: `tests/test_web_settings.py`

**Interfaces:**
- Consumes: `unresolved_carrier_names`, `resolve_carrier`, `add_alias`, `set_admitted`, model `PolicyBillingType`.
- Produces: routes `POST /settings/carriers`, `POST /settings/carriers/{id}/alias`, `POST /settings/carriers/{id}/admitted`, `POST /settings/policies/{id}/billing-type`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_settings.py — append
from renewal.carriers import admitted_status, resolve_carrier
from renewal.models import Carrier, PolicyBillingType


def test_unresolved_carrier_names_are_listed(client_app, policy_with_odd_carrier):
    assert "Some Other Insurance Company" in client_app.get("/settings").text


def test_creating_a_carrier_from_the_settings_page(client_app, db):
    client_app.post("/settings/carriers",
                    data={"display_name": "Scottsdale Insurance Company"},
                    follow_redirects=False)
    assert db.query(Carrier).filter_by(
        display_name="Scottsdale Insurance Company").count() == 1


def test_adding_an_alias_resolves_the_odd_name(client_app, db, carrier_id):
    client_app.post(f"/settings/carriers/{carrier_id}/alias",
                    data={"alias": "Some Other Insurance Company"},
                    follow_redirects=False)
    assert resolve_carrier(db, "Some Other Insurance Company").id == carrier_id


def test_setting_admitted_status_records_the_state(client_app, db, carrier_id):
    client_app.post(f"/settings/carriers/{carrier_id}/admitted",
                    data={"state": "ca", "status": "non_admitted"},
                    follow_redirects=False)
    assert admitted_status(db, carrier_id, "CA") == "non_admitted"


def test_an_invalid_admitted_status_is_rejected(client_app, carrier_id):
    response = client_app.post(f"/settings/carriers/{carrier_id}/admitted",
                               data={"state": "CA", "status": "probably fine"},
                               follow_redirects=False)
    assert response.status_code == 422


def test_setting_billing_type_appends(client_app, db, policy_id):
    client_app.post(f"/settings/policies/{policy_id}/billing-type",
                    data={"billing_type": "direct_bill"}, follow_redirects=False)
    client_app.post(f"/settings/policies/{policy_id}/billing-type",
                    data={"billing_type": "agency_bill"}, follow_redirects=False)
    rows = db.query(PolicyBillingType).filter_by(policy_id=policy_id).order_by(
        PolicyBillingType.id).all()
    assert [r.billing_type for r in rows] == ["direct_bill", "agency_bill"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_settings.py -v`
Expected: FAIL with 404 on `/settings/carriers`

- [ ] **Step 3: Implement the routes**

In `renewal/web/settings.py`:

- `GET /settings` — extend the existing page with a carriers section listing every `Carrier` with its per-state admitted rows, and a separate list from `unresolved_carrier_names(session)` with an "alias to…" control per name.
- `POST /settings/carriers` — create a `Carrier` from `display_name`; 409 if the normalized name already exists.
- `POST /settings/carriers/{id}/alias` — `add_alias`; 409 on a duplicate alias.
- `POST /settings/carriers/{id}/admitted` — validate `status` against `("admitted", "non_admitted", "unknown")` and return 422 otherwise, validate `state` as two letters, then `set_admitted`.
- `POST /settings/policies/{id}/billing-type` — validate against `("direct_bill", "agency_bill", "unknown")`, insert a `PolicyBillingType` row.

Every one of these is a human decision recorded as a row. None of them infers anything from a document.

- [ ] **Step 4: Update the template**

Add the carriers section to `settings.html`. Each unresolved name shows a select of existing carriers plus a "create as new carrier" button, so aliasing is one interaction. The admitted-status control is a state field and a three-way select whose default is `unknown`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_settings.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add renewal/web/settings.py renewal/templates/settings.html tests/test_web_settings.py
git commit -m "feat(web): carrier aliasing, admitted status, and billing type controls"
```

---

## Task 3: Migration — search indexes

Indexes only. No tables, no columns.

**Files:**
- Create: `migrations/versions/<generated>_search_indexes.py`
- Test: `tests/test_search_indexes.py` (create)

**Interfaces:**
- Produces: `ix_client_display_name_trgm`, `ix_carrier_display_name_trgm`, `ix_policy_policy_number`, `ix_document_uploaded_at_desc`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_indexes.py
from sqlalchemy import text

EXPECTED = {
    "ix_client_display_name_trgm",
    "ix_carrier_display_name_trgm",
    "ix_policy_policy_number",
    "ix_document_uploaded_at_desc",
    "ix_document_text_tsv",
}


def test_the_search_indexes_exist(session):
    found = {
        row[0] for row in session.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
    }
    assert EXPECTED <= found


def test_the_text_index_is_gin(session):
    definition = session.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_document_text_tsv'"
    )).scalar()
    assert "gin" in definition.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_search_indexes.py -v`
Expected: FAIL — the four new index names are absent

- [ ] **Step 3: Generate and write the migration**

Run: `alembic revision -m "search indexes"`

```python
def upgrade() -> None:
    op.create_index(
        "ix_client_display_name_trgm", "client", ["display_name"],
        postgresql_using="gin", postgresql_ops={"display_name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_carrier_display_name_trgm", "carrier", ["display_name"],
        postgresql_using="gin", postgresql_ops={"display_name": "gin_trgm_ops"},
    )
    op.create_index("ix_policy_policy_number", "policy", ["policy_number"])
    op.create_index(
        "ix_document_uploaded_at_desc", "document",
        [sa.text("uploaded_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_document_uploaded_at_desc", table_name="document")
    op.drop_index("ix_policy_policy_number", table_name="policy")
    op.drop_index("ix_carrier_display_name_trgm", table_name="carrier")
    op.drop_index("ix_client_display_name_trgm", table_name="client")
```

`pg_trgm` is already installed by the Plan A migration that created
`document_text`, so nothing here creates an extension.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_search_indexes.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/ tests/test_search_indexes.py
git commit -m "feat(db): indexes for full-text and name search"
```

---

## Task 4: The search query

Across document text, client names, policy numbers, and carrier names. Newest first, as specified — not by rank, because she is usually looking for the most recent thing about someone.

**Files:**
- Create: `renewal/search/__init__.py`, `renewal/search/query.py`
- Test: `tests/test_search.py` (create)

**Interfaces:**
- Consumes: models `DocumentText`, `Document`, `DocumentLink`, `DocumentClassification`, `Client`, `Policy`, `Carrier`.
- Produces: `SearchResult(document_id, client_id, client_name, doc_class, uploaded_at, carrier_name, snippet, matched_on)`; `search(session, q: str, *, client_id=None, doc_class=None, start=None, end=None, limit=50, offset=0) -> list[SearchResult]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search.py
from datetime import date

from renewal.models import Client, Carrier, DocumentClassification, DocumentLink, Policy
from renewal.search.query import search


def _seed(session, store, text_lines, client_name="Acme Landscaping LLC"):
    from renewal.pipeline import ingest_document
    from tests.pdfmaker import make_text_pdf
    from tests.test_dates_llm import StubClient, _settings

    client = Client(display_name=client_name)
    session.add(client)
    session.flush()
    document = ingest_document(
        session, store, data=make_text_pdf([text_lines]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=StubClient('{"dates": []}'), settings=_settings(),
    )
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="manual", confidence=1.0, candidates=[]))
    session.flush()
    return client, document


def test_finds_a_document_by_a_word_in_its_text(session, store):
    _, document = _seed(session, store, ["NOTICE OF CANCELLATION", "effective soon"])
    results = search(session, "cancellation")
    assert [r.document_id for r in results] == [document.id]


def test_finds_a_document_by_its_clients_name(session, store):
    _, document = _seed(session, store, ["a page with no distinguishing words"])
    assert [r.document_id for r in search(session, "Acme Landscaping")] == [document.id]


def test_finds_a_document_by_policy_number(session, store):
    client, document = _seed(session, store, ["nothing useful here"])
    policy = Policy(client_id=client.id, carrier_name="Travelers",
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    # Append a corrected link rather than editing the first one. Tests model the
    # invariant too; a test that updates teaches the wrong pattern.
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             policy_id=policy.id, method="manual",
                             confidence=1.0, candidates=[]))
    session.flush()
    assert [r.document_id for r in search(session, "CAP-7781-22")] == [document.id]


def test_the_snippet_highlights_the_term(session, store):
    _seed(session, store, ["NOTICE OF CANCELLATION effective 07/01/2026"])
    snippet = search(session, "cancellation")[0].snippet
    assert "<b>" in snippet.lower() or "<mark>" in snippet.lower()


def test_results_are_newest_first(session, store):
    _, first = _seed(session, store, ["cancellation one"])
    _, second = _seed(session, store, ["cancellation two"], "Other Client LLC")
    assert [r.document_id for r in search(session, "cancellation")] == [
        second.id, first.id]


def test_filters_by_client(session, store):
    client, document = _seed(session, store, ["cancellation one"])
    _seed(session, store, ["cancellation two"], "Other Client LLC")
    assert [r.document_id for r in search(session, "cancellation",
                                          client_id=client.id)] == [document.id]


def test_filters_by_document_class(session, store):
    _, document = _seed(session, store, ["cancellation one"])
    session.add(DocumentClassification(document_id=document.id,
                                       doc_class="cancellation_notice",
                                       confidence=0.9,
                                       classifier_version="classify-v1",
                                       model_id="stub"))
    session.flush()
    assert len(search(session, "cancellation", doc_class="cancellation_notice")) == 1
    assert search(session, "cancellation", doc_class="invoice") == []


def test_an_unclassified_document_is_still_findable(session, store):
    """Search must never depend on classification having run."""
    _, document = _seed(session, store, ["cancellation one"])
    assert [r.document_id for r in search(session, "cancellation")] == [document.id]


def test_an_unmatched_document_is_still_findable(session, store):
    """Nor on client resolution having succeeded."""
    from renewal.pipeline import ingest_document
    from tests.pdfmaker import make_text_pdf
    from tests.test_dates_llm import StubClient, _settings
    document = ingest_document(
        session, store, data=make_text_pdf([["orphan cancellation notice"]]),
        original_filename="d.pdf", source="bulk_import", agency_id=1,
        model_client=StubClient('{"dates": []}'), settings=_settings(),
    )
    result = search(session, "cancellation")[0]
    assert result.document_id == document.id
    assert result.client_name is None


def test_multi_word_queries_are_handled_as_a_phrase_search(session, store):
    _seed(session, store, ["NOTICE OF CANCELLATION"])
    assert search(session, '"notice of cancellation"')
    assert search(session, "notice cancellation")


def test_a_query_with_no_hits_returns_nothing_rather_than_everything(session, store):
    _seed(session, store, ["cancellation one"])
    assert search(session, "zzzznotaword") == []


def test_punctuation_in_a_query_does_not_error(session, store):
    """websearch_to_tsquery tolerates what plainto_tsquery does not."""
    _seed(session, store, ["cancellation one"])
    assert search(session, "cancellation & | ! ()") is not None


def test_one_document_appears_once_even_when_it_matches_two_ways(session, store):
    _seed(session, store, ["Acme Landscaping cancellation"])
    assert len(search(session, "Acme")) == 1


def test_a_deduplicated_document_matching_on_text_records_why(session, store):
    _seed(session, store, ["NOTICE OF CANCELLATION"])
    assert "text" in search(session, "cancellation")[0].matched_on
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_search.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.search'`

- [ ] **Step 3: Implement**

```python
# renewal/search/query.py
"""The one search query.

Four ways in — page text, client name, policy number, carrier name — unioned to
one row per document. A document is findable whether or not it was classified
and whether or not it was matched to a client, because those paths involve
judgment and this one must not.

Ordered newest first rather than by rank: when she is on the phone she is
almost always after the most recent thing about someone, not the best textual
match.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import String, func, literal, or_, select
from sqlalchemy.orm import Session

from renewal.models import (
    Carrier, Client, Document, DocumentClassification, DocumentLink,
    DocumentText, Policy,
)

NAME_SIMILARITY_FLOOR = 0.3


@dataclass(frozen=True)
class SearchResult:
    document_id: int
    client_id: int | None
    client_name: str | None
    doc_class: str | None
    uploaded_at: datetime
    carrier_name: str | None
    snippet: str
    matched_on: tuple[str, ...]


def _latest(model, fk: str, value_column: str):
    column = getattr(model, fk)
    ranked = select(
        column.label("parent_id"),
        getattr(model, value_column).label("value"),
        func.row_number()
        .over(partition_by=column, order_by=model.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.parent_id, ranked.c.value).where(
        ranked.c.rn == 1
    ).subquery()


def search(
    session: Session,
    q: str,
    *,
    client_id: int | None = None,
    doc_class: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SearchResult]:
    if not q.strip():
        return []

    tsquery = func.websearch_to_tsquery("english", q)
    links = _latest(DocumentLink, "document_id", "client_id")
    classes = _latest(DocumentClassification, "document_id", "doc_class")

    text_hits = select(DocumentText.document_id).where(
        DocumentText.tsv.op("@@")(tsquery)
    )
    name_hits = (
        select(links.c.parent_id)
        .join(Client, Client.id == links.c.value)
        .where(func.similarity(Client.display_name, q) > NAME_SIMILARITY_FLOOR)
    )
    policy_hits = (
        select(DocumentLink.document_id)
        .join(Policy, Policy.id == DocumentLink.policy_id)
        .where(func.upper(Policy.policy_number) == q.strip().upper())
    )
    carrier_hits = (
        select(DocumentLink.document_id)
        .join(Policy, Policy.id == DocumentLink.policy_id)
        .join(Carrier, func.lower(Carrier.display_name) == func.lower(
            Policy.carrier_name))
        .where(func.similarity(Carrier.display_name, q) > NAME_SIMILARITY_FLOOR)
    )

    matched = text_hits.union(name_hits, policy_hits, carrier_hits).subquery()

    snippet = func.ts_headline(
        "english",
        func.coalesce(
            select(DocumentText.text)
            .where(DocumentText.document_id == Document.id)
            .where(DocumentText.tsv.op("@@")(tsquery))
            .limit(1)
            .scalar_subquery(),
            literal("", String),
        ),
        tsquery,
        literal("StartSel=<mark>, StopSel=</mark>, MaxFragments=1, MaxWords=25"),
    )

    query = (
        select(
            Document.id, Document.uploaded_at, links.c.value, Client.display_name,
            classes.c.value, Policy.carrier_name, snippet,
        )
        .join(matched, matched.c.document_id == Document.id)
        .outerjoin(links, links.c.parent_id == Document.id)
        .outerjoin(Client, Client.id == links.c.value)
        .outerjoin(classes, classes.c.parent_id == Document.id)
        .outerjoin(DocumentLink, DocumentLink.document_id == Document.id)
        .outerjoin(Policy, Policy.id == DocumentLink.policy_id)
        .order_by(Document.uploaded_at.desc(), Document.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if client_id is not None:
        query = query.where(links.c.value == client_id)
    if doc_class is not None:
        query = query.where(classes.c.value == doc_class)
    if start is not None:
        query = query.where(Document.uploaded_at >= start)
    if end is not None:
        query = query.where(Document.uploaded_at <= end)

    seen: set[int] = set()
    out: list[SearchResult] = []
    for row in session.execute(query):
        document_id = row[0]
        if document_id in seen:
            continue
        seen.add(document_id)
        out.append(
            SearchResult(
                document_id=document_id,
                uploaded_at=row[1],
                client_id=row[2],
                client_name=row[3],
                doc_class=row[4],
                carrier_name=row[5],
                snippet=row[6] or "",
                matched_on=("text",) if row[6] else ("name",),
            )
        )
    return out
```

`websearch_to_tsquery` rather than `plainto_tsquery`: it accepts quoted phrases
and bare operator characters without raising, so a query she types with a stray
`&` returns results instead of a 500.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_search.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/search/ tests/test_search.py
git commit -m "feat(search): full-text and name search across the record"
```

---

## Task 5: The search screen and its performance budget

**Files:**
- Create: `renewal/web/search.py`, `renewal/templates/search.html`, `tests/test_search_perf.py`
- Modify: `renewal/web/__init__.py`, `pyproject.toml`
- Test: `tests/test_web_search.py` (create)

**Interfaces:**
- Consumes: `search`.
- Produces: route `GET /search`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_search.py
def test_the_box_renders_empty(client_app):
    response = client_app.get("/search")
    assert response.status_code == 200
    assert "<form" in response.text


def test_a_query_returns_results_with_a_highlighted_snippet(client_app, seeded_doc):
    body = client_app.get("/search?q=cancellation").text
    assert "<mark>" in body
    assert "Acme Landscaping LLC" in body


def test_the_snippet_is_escaped_apart_from_the_highlight(client_app, seeded_script):
    """ts_headline returns markup, so the rest must not be trusted as HTML."""
    body = client_app.get("/search?q=payload").text
    assert "<script>" not in body


def test_no_results_says_so(client_app, seeded_doc):
    assert "no matches" in client_app.get("/search?q=zzzznotaword").text.lower()


def test_filters_are_reflected_back_into_the_form(client_app, seeded_doc):
    body = client_app.get("/search?q=cancellation&doc_class=invoice").text
    assert 'value="cancellation"' in body
```

```python
# tests/test_search_perf.py
"""The 300ms budget as a test rather than an aspiration.

Marked `perf` and excluded from the default run: it seeds ten thousand
documents, which takes far longer than the assertion it makes.
Run with: pytest -m perf tests/test_search_perf.py -s
"""

import time

import pytest

from renewal.models import Client, Document, DocumentText
from renewal.search.query import search

BUDGET_SECONDS = 0.3
DOCUMENTS = 10_000


@pytest.mark.perf
def test_search_stays_under_the_budget_at_ten_thousand_documents(session, capsys):
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    session.bulk_save_objects([
        Document(blob_sha256=f"{i:064x}", original_filename=f"{i}.pdf",
                 page_count=1, has_text_layer=True, doc_type="dec_page",
                 source="bulk_import", agency_id=1)
        for i in range(DOCUMENTS)
    ])
    session.flush()
    ids = [row[0] for row in session.execute(
        __import__("sqlalchemy").select(Document.id))]
    session.bulk_save_objects([
        DocumentText(document_id=document_id, page_number=1,
                     text=f"policy declarations page number {document_id} "
                          f"cancellation notice effective 07/01/2026",
                     extraction_method="pymupdf", extractor_version="text-v1")
        for document_id in ids
    ])
    session.flush()
    session.execute(__import__("sqlalchemy").text("ANALYZE document_text"))

    started = time.perf_counter()
    results = search(session, "cancellation", limit=50)
    elapsed = time.perf_counter() - started

    with capsys.disabled():
        print(f"\nsearch over {DOCUMENTS} documents: {1000 * elapsed:.0f}ms")
    assert results
    assert elapsed < BUDGET_SECONDS
```

- [ ] **Step 2: Register the marker and run the tests to verify they fail**

Add `"perf: seeds a large dataset; excluded from the default run"` to `markers`
in `pyproject.toml` and change `addopts` to `-m 'not eval and not perf'`.

Run: `pytest tests/test_web_search.py -v`
Expected: FAIL with 404 on `/search`

- [ ] **Step 3: Implement the route**

`renewal/web/search.py`: `GET /search` reads `q`, `client_id`, `doc_class`,
`start`, `end` from the query string, calls `search`, and renders
`search.html`. An empty `q` renders the form with no results section rather
than every document.

- [ ] **Step 4: Write the template**

One box, autofocused, at the top. Each result is one dense line: client name (or
"unmatched"), document class (or "unclassified"), upload date, carrier, and the
snippet.

The snippet contains `<mark>` tags from `ts_headline` and nothing else may be
trusted: escape the string, then unescape only `&lt;mark&gt;` and
`&lt;/mark&gt;` back to tags. Do not mark the whole snippet safe — it is
document text, and document text is not ours.

Filters sit beside the box and their current values are reflected back into the
form so a refinement does not lose the query.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_search.py -v`
Expected: 5 passed

- [ ] **Step 6: Run the performance test**

Run: `pytest -m perf tests/test_search_perf.py -s`
Expected: the elapsed line prints and the assertion passes. If it does not,
`EXPLAIN ANALYZE` the query before adding indexes — the likely culprit is the
`ts_headline` correlated subquery running per row rather than per result.

- [ ] **Step 7: Commit**

```bash
git add renewal/web/search.py renewal/templates/search.html tests/test_web_search.py tests/test_search_perf.py pyproject.toml
git commit -m "feat(web): the search screen, with a 300ms budget as a test"
```

---

## Task 6: The client overview query

Everything about one client, assembled in one place, optimized for scanning under time pressure.

**Files:**
- Create: `renewal/clients/__init__.py`, `renewal/clients/overview.py`
- Test: `tests/test_client_overview.py` (create)

**Interfaces:**
- Consumes: `agenda`, `admitted_status`, `resolve_carrier`, models `Policy`, `PolicyTerm`, `PolicyBillingType`, `Document`, `DocumentLink`, `DocumentClassification`, `InboundMessage`, `AttentionItem`, `AttentionEvent`.
- Produces: `PolicyRow(policy_id, carrier_name, policy_number, line_of_business, state, effective_date, expiration_date, total_premium, admitted, billing_type, billing_type_from_term, billing_mismatch, days_to_renewal)`; `DocumentRow(document_id, original_filename, uploaded_at, doc_class, source)`; `ClientOverview(client, policies, upcoming_dates, documents, messages, attention)`; `overview(session, client_id: int, *, agency_id: int, today: date | None = None) -> ClientOverview`; `RENEWAL_WINDOW_DAYS = 60`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client_overview.py
from datetime import date, timedelta

import pytest

from renewal.carriers import set_admitted
from renewal.clients.overview import RENEWAL_WINDOW_DAYS, overview
from renewal.models import (
    Carrier, Client, Document, DocumentDate, DocumentLink, Policy,
    PolicyBillingType, PolicyTerm,
)

TODAY = date(2026, 6, 1)


def _client(session, name="Acme Landscaping LLC"):
    client = Client(display_name=name)
    session.add(client)
    session.flush()
    return client


def _policy(session, client, *, carrier="Travelers", state="CA", expires=None):
    policy = Policy(client_id=client.id, carrier_name=carrier,
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto", state=state)
    session.add(policy)
    session.flush()
    session.add(PolicyTerm(policy_id=policy.id, carrier_name=carrier,
                           effective_date=date(2025, 7, 1),
                           expiration_date=expires or date(2026, 7, 1),
                           total_premium="4820.00"))
    session.flush()
    return policy


def test_policies_are_listed_with_their_term_dates(session):
    client = _client(session)
    _policy(session, client)
    got = overview(session, client.id, agency_id=1, today=TODAY)
    assert got.policies[0].expiration_date == date(2026, 7, 1)
    assert got.policies[0].total_premium == "4820.00"


def test_admitted_status_is_read_for_the_policys_state(session):
    client = _client(session)
    _policy(session, client, carrier="Scottsdale Insurance Company", state="CA")
    carrier = Carrier(display_name="Scottsdale Insurance Company")
    session.add(carrier)
    session.flush()
    set_admitted(session, carrier.id, "CA", "non_admitted")
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].admitted == "non_admitted"


def test_an_unresolvable_carrier_is_unknown_not_assumed(session):
    client = _client(session)
    _policy(session, client, carrier="Some Company We Have Never Seen")
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].admitted == "unknown"


def test_billing_type_defaults_to_unknown(session):
    client = _client(session)
    _policy(session, client)
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].billing_type == "unknown"


def test_her_billing_value_wins_and_a_disagreeing_term_is_flagged(session):
    """A disagreement means billing changed at renewal, which is worth seeing."""
    client = _client(session)
    policy = _policy(session, client)
    session.add(PolicyBillingType(policy_id=policy.id,
                                  billing_type="agency_bill", set_by="human"))
    term = session.query(PolicyTerm).filter_by(policy_id=policy.id).one()
    term.billing_type = "direct_bill"
    session.flush()
    row = overview(session, client.id, agency_id=1, today=TODAY).policies[0]
    assert row.billing_type == "agency_bill"
    assert row.billing_type_from_term == "direct_bill"
    assert row.billing_mismatch is True


def test_agreeing_values_are_not_flagged(session):
    client = _client(session)
    policy = _policy(session, client)
    session.add(PolicyBillingType(policy_id=policy.id,
                                  billing_type="direct_bill", set_by="human"))
    term = session.query(PolicyTerm).filter_by(policy_id=policy.id).one()
    term.billing_type = "direct_bill"
    session.flush()
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].billing_mismatch is False


def test_renewal_countdown_inside_the_window(session):
    client = _client(session)
    _policy(session, client, expires=TODAY + timedelta(days=30))
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].days_to_renewal == 30


def test_no_countdown_outside_the_window(session):
    client = _client(session)
    _policy(session, client, expires=TODAY + timedelta(days=RENEWAL_WINDOW_DAYS + 1))
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].days_to_renewal is None


def test_an_expired_term_shows_a_negative_countdown(session):
    """She needs to see the one she missed, not have it hidden."""
    client = _client(session)
    _policy(session, client, expires=TODAY - timedelta(days=3))
    assert overview(session, client.id, agency_id=1,
                    today=TODAY).policies[0].days_to_renewal == -3


def test_upcoming_dates_carry_their_confirmation_status(session):
    client = _client(session)
    document = Document(blob_sha256="a" * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="auto", confidence=1.0, candidates=[]))
    session.add(DocumentDate(document_id=document.id, date_value=date(2026, 7, 1),
                             date_type="policy_expiration", source_page=1,
                             source_text="x", confidence=0.5,
                             extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    got = overview(session, client.id, agency_id=1, today=TODAY)
    assert got.upcoming_dates[0].status == "unconfirmed"


def test_documents_are_newest_first(session):
    client = _client(session)
    for name in ("old.pdf", "new.pdf"):
        document = Document(blob_sha256=name.ljust(64, "0")[:64].replace(".", "0"),
                            original_filename=name, page_count=1,
                            has_text_layer=True, doc_type="dec_page",
                            source="bulk_import", agency_id=1)
        session.add(document)
        session.flush()
        session.add(DocumentLink(document_id=document.id, client_id=client.id,
                                 method="auto", confidence=1.0, candidates=[]))
        session.flush()
    names = [d.original_filename
             for d in overview(session, client.id, agency_id=1,
                               today=TODAY).documents]
    assert names == ["new.pdf", "old.pdf"]


def test_a_client_with_nothing_renders_empty_rather_than_erroring(session):
    client = _client(session)
    got = overview(session, client.id, agency_id=1, today=TODAY)
    assert got.policies == []
    assert got.documents == []
    assert got.attention == []


def test_an_unknown_client_raises(session):
    with pytest.raises(LookupError):
        overview(session, 999999, agency_id=1, today=TODAY)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_client_overview.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'renewal.clients'`

- [ ] **Step 3: Implement**

```python
# renewal/clients/overview.py
"""Everything about one client, assembled for one screen.

Built for reading while she is on the phone: dense, complete, and never hiding
a basic fact behind a click. Every value it shows is either a stored fact or an
explicit 'unknown' — nothing here infers, and nothing here guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.calendarview.agenda import AgendaEntry, agenda
from renewal.carriers import admitted_status, resolve_carrier
from renewal.models import (
    AttentionEvent, AttentionItem, Client, Document, DocumentClassification,
    DocumentLink, InboundMessage, Policy, PolicyBillingType, PolicyTerm,
)

RENEWAL_WINDOW_DAYS = 60
DOCUMENT_LIMIT = 200
MESSAGE_LIMIT = 10


@dataclass(frozen=True)
class PolicyRow:
    policy_id: int
    carrier_name: str
    policy_number: str
    line_of_business: str
    state: str | None
    effective_date: date | None
    expiration_date: date | None
    total_premium: str | None
    admitted: str
    billing_type: str
    billing_type_from_term: str | None
    billing_mismatch: bool
    days_to_renewal: int | None


@dataclass(frozen=True)
class DocumentRow:
    document_id: int
    original_filename: str
    uploaded_at: datetime
    doc_class: str | None
    source: str


@dataclass(frozen=True)
class ClientOverview:
    client: Client
    policies: list[PolicyRow]
    upcoming_dates: list[AgendaEntry]
    documents: list[DocumentRow]
    messages: list[InboundMessage]
    attention: list[AttentionItem]


def _latest_term(session: Session, policy_id: int) -> PolicyTerm | None:
    return session.scalar(
        select(PolicyTerm)
        .where(PolicyTerm.policy_id == policy_id)
        .order_by(PolicyTerm.id.desc())
        .limit(1)
    )


def _billing_type(session: Session, policy_id: int) -> str:
    return session.scalar(
        select(PolicyBillingType.billing_type)
        .where(PolicyBillingType.policy_id == policy_id)
        .order_by(PolicyBillingType.id.desc())
        .limit(1)
    ) or "unknown"


def _countdown(expiration: date | None, today: date) -> int | None:
    """None outside the window. An expired term returns a negative number
    rather than None: the renewal she missed is the one she most needs to see."""
    if expiration is None:
        return None
    days = (expiration - today).days
    return days if days <= RENEWAL_WINDOW_DAYS else None


def overview(
    session: Session, client_id: int, *, agency_id: int, today: date | None = None
) -> ClientOverview:
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError(f"no client with id {client_id}")
    today = today or date.today()

    policies: list[PolicyRow] = []
    for policy in session.scalars(
        select(Policy).where(Policy.client_id == client_id).order_by(Policy.id)
    ):
        term = _latest_term(session, policy.id)
        carrier = resolve_carrier(session, policy.carrier_name)
        hers = _billing_type(session, policy.id)
        from_term = term.billing_type if term else None
        policies.append(
            PolicyRow(
                policy_id=policy.id,
                carrier_name=policy.carrier_name,
                policy_number=policy.policy_number,
                line_of_business=policy.line_of_business,
                state=policy.state,
                effective_date=term.effective_date if term else None,
                expiration_date=term.expiration_date if term else None,
                total_premium=term.total_premium if term else None,
                admitted=(
                    admitted_status(session, carrier.id, policy.state)
                    if carrier else "unknown"
                ),
                billing_type=hers,
                billing_type_from_term=from_term,
                billing_mismatch=bool(
                    from_term and hers != "unknown" and from_term != hers
                ),
                days_to_renewal=_countdown(
                    term.expiration_date if term else None, today
                ),
            )
        )

    links = select(DocumentLink.document_id).where(
        DocumentLink.client_id == client_id
    )
    classes = select(
        DocumentClassification.document_id, DocumentClassification.doc_class
    ).subquery()
    documents = [
        DocumentRow(document_id=row[0], original_filename=row[1],
                    uploaded_at=row[2], doc_class=row[3], source=row[4])
        for row in session.execute(
            select(Document.id, Document.original_filename, Document.uploaded_at,
                   classes.c.doc_class, Document.source)
            .outerjoin(classes, classes.c.document_id == Document.id)
            .where(Document.id.in_(links))
            .order_by(Document.uploaded_at.desc(), Document.id.desc())
            .limit(DOCUMENT_LIMIT)
        )
    ]

    resolved = select(AttentionEvent.attention_item_id).distinct()
    attention = list(
        session.scalars(
            select(AttentionItem)
            .where(AttentionItem.document_id.in_(links))
            .where(AttentionItem.id.not_in(resolved))
            .order_by(AttentionItem.id.desc())
        )
    )

    messages = list(
        session.scalars(
            select(InboundMessage)
            .where(InboundMessage.id.in_(
                select(Document.inbound_message_id).where(Document.id.in_(links))
            ))
            .order_by(InboundMessage.received_at.desc())
            .limit(MESSAGE_LIMIT)
        )
    )

    return ClientOverview(
        client=client,
        policies=policies,
        upcoming_dates=agenda(session, agency_id=agency_id, client_id=client_id,
                              start=today),
        documents=documents,
        messages=messages,
        attention=attention,
    )
```

`attention` and `messages` are empty until Plan C fills those tables. The
queries are written now so the screen does not need reworking then.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_client_overview.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/clients/ tests/test_client_overview.py
git commit -m "feat(clients): assemble everything about one client"
```

---

## Task 7: The client overview screen

Dense, no pagination above the fold, no clicking to reveal basics.

**Files:**
- Create: `renewal/web/clients.py`, `renewal/templates/client.html`
- Modify: `renewal/web/__init__.py`, `renewal/static/app.css`
- Test: `tests/test_web_clients.py` (create)

**Interfaces:**
- Consumes: `overview`.
- Produces: routes `GET /clients`, `GET /clients/{id}`, `GET /documents/{id}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_clients.py
def test_the_client_list_renders(client_app, seeded_client):
    assert "Acme Landscaping LLC" in client_app.get("/clients").text


def test_the_overview_shows_policies_with_admitted_and_billing(
    client_app, seeded_client
):
    body = client_app.get(f"/clients/{seeded_client}").text
    assert "CAP-7781-22" in body
    assert "non-admitted" in body.lower()
    assert "direct bill" in body.lower()


def test_a_billing_mismatch_is_visible(client_app, seeded_mismatch):
    assert "mismatch" in client_app.get(f"/clients/{seeded_mismatch}").text.lower()


def test_an_unconfirmed_upcoming_date_is_flagged(client_app, seeded_client):
    assert "unconfirmed" in client_app.get(f"/clients/{seeded_client}").text.lower()


def test_a_renewal_inside_sixty_days_shows_a_countdown(client_app, seeded_renewal):
    assert "days" in client_app.get(f"/clients/{seeded_renewal}").text.lower()


def test_documents_link_to_the_pdf(client_app, seeded_client):
    assert "/documents/" in client_app.get(f"/clients/{seeded_client}").text


def test_the_pdf_route_serves_the_blob(client_app, seeded_document):
    response = client_app.get(f"/documents/{seeded_document}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_a_missing_client_is_a_404_not_a_500(client_app):
    assert client_app.get("/clients/999999").status_code == 404


def test_a_client_with_nothing_renders(client_app, empty_client):
    assert client_app.get(f"/clients/{empty_client}").status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_clients.py -v`
Expected: FAIL with 404 on `/clients`

- [ ] **Step 3: Implement the routes**

`renewal/web/clients.py`:

- `GET /clients` — every client, alphabetical, with a policy count and the
  nearest upcoming date. This is the way into the overview.
- `GET /clients/{id}` — call `overview`, catch `LookupError` and return 404,
  render `client.html`.
- `GET /documents/{id}` — read the blob through the keyed `BlobStore` and
  return it with `media_type="application/pdf"` and a
  `Content-Disposition: inline` header carrying the original filename. 404 when
  the document or its blob is missing. This is the route every "one click to
  the PDF" link in the app points at.

- [ ] **Step 4: Write the template**

`client.html`, top to bottom, everything visible without interaction:

1. **Policies** — one row each: carrier, policy number, line of business, term
   dates, premium, admitted status, billing type. Admitted status renders as
   the words `admitted`, `non-admitted`, and `unknown` — hyphenated for
   reading, not the stored `non_admitted`. Billing type renders as
   `direct bill`, `agency bill`, and `unknown`. `unknown` renders as the word
   `unknown`, never as blank, because blank reads as "nothing to worry about".
   A `billing_mismatch` row carries a visible marker naming both values. A
   `days_to_renewal` that is not None renders as a countdown, and a negative
   one renders as overdue rather than as a small number.
2. **Upcoming dates** — from `upcoming_dates`, unconfirmed ones marked exactly
   as they are on the calendar so the two screens agree.
3. **Attention** — open items, empty until Plan C.
4. **Recent messages** — empty until Plan C.
5. **Documents** — newest first, filename, date, class, one click to the PDF.

Add the styles for the mismatch marker and the overdue countdown to `app.css`,
reusing the `unconfirmed` and `escalated` classes from the calendar rather than
inventing parallel ones.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_clients.py -v`
Expected: 9 passed

- [ ] **Step 6: Verify by hand against her archive**

Load a real client's overview. Confirm you can answer, without scrolling or
clicking: which policies they have, when the next one renews, whether each
carrier is admitted, and whether anything is unconfirmed.

- [ ] **Step 7: Commit**

```bash
git add renewal/web/clients.py renewal/templates/client.html renewal/static/app.css tests/test_web_clients.py
git commit -m "feat(web): the client overview screen"
```

---

## Done when

- [ ] `pytest tests/ -v` passes.
- [ ] `pytest -m perf tests/test_search_perf.py -s` reports under 300ms.
- [ ] Search finds a known document from her archive by a word in its body, by client name, and by policy number.
- [ ] A client overview answers the phone-call questions without a click.

Plan C is next: inbound email intake, classification, and the attention queue.
