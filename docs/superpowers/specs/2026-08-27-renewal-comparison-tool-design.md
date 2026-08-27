# Renewal Comparison Tool — v0 Design

**Date:** 2026-08-27
**Status:** Approved for implementation planning
**Author:** brainstormed with Claude Code

---

## 1. Context

Small independent US P&C agencies (1–5 people) have no agency management system, no
IVANS/carrier download, and no ops staff. Carrier documents arrive as PDF email attachments and
live in an inbox and a folder tree.

The workflow this tool attacks: **when a policy renews, the agency must explain to the client
what changed and why the premium moved.** Today that means manually comparing two declarations
pages. It usually doesn't happen properly, so the client calls angry and the agent improvises.

The tool takes the prior term's dec page and the renewal dec page, extracts both, diffs them,
decides which differences a client would care about, and produces a draft explanation the agent
reviews and sends.

**One user (the author), one design-partner agency.** Optimize for learning speed and data
quality, not scale.

## 2. Non-negotiable constraints

These come from the domain and are not open to optimization.

1. **Nothing is ever sent automatically.** Output is always a draft a licensed human reads and
   edits. A silently wrong effective date that lets a policy lapse is an E&O claim against a
   real agency.
2. **Nothing is ever deleted or overwritten.** Append-only everywhere. Record-retention rules
   and E&O defense both depend on the file being complete and immutable.
3. **Every extracted value is traceable to its source** — document, page, and the verbatim text
   span. A field whose origin can't be answered is worthless.
4. **Every human correction is captured as training data.** This is the point of the project.
5. **Extraction is a pure, versioned, re-runnable function.** Any extractor version must be
   re-runnable across every document ever seen, with old and new results comparable.

## 3. Scope

**In scope for v0:** ingest two PDFs via a minimal web UI; extract structured policy data from
each; diff; classify by materiality; generate a plain-English draft; a review screen for
correcting any extracted field and editing the draft; persist everything including corrections.

**Explicitly out of scope.** Do not build without asking first: authentication, accounts, roles,
multi-tenancy; billing; email sending or inbox integration; ACORD form generation; carrier
API/IVANS/AL3; a design system or component library; mobile responsiveness; background job
queues, websockets, real-time anything; Docker, CI, deployment config; abstraction layers for
hypothetical future carriers or lines of business.

The frontend is as boring as possible. Server-rendered HTML. No React.

## 4. Tech stack

- Python 3.11+, FastAPI
- PostgreSQL (local), SQLAlchemy + Alembic migrations
- PyMuPDF for text extraction and page rasterization
- Anthropic API for extraction and draft generation
- Jinja2 templates, minimal hand-written CSS
- pytest for unit tests and the eval harness

Postgres from day one rather than SQLite: JSONB for raw extraction payloads, real full-text
search, and the option to add pgvector later without a migration.

**Models:** `claude-opus-5` for extraction (accuracy matters, volume is low), `claude-sonnet-5`
for draft generation. Both configurable; the model actually used is recorded per-extraction in
`extraction.model_id`. Temperature 0 everywhere.

## 5. Architecture

The pipeline is a plain Python library. FastAPI routes are thin callers that parse a request and
call the library. The eval harness calls the same functions directly. No pipeline logic lives in
a route handler.

```
renewal/
  blobstore.py    put(bytes)->sha256, get(sha256)->bytes.  Local filesystem only.
  db.py           engine, session management
  models.py       SQLAlchemy models, insert-only
  ingest.py       bytes -> blob + document row (dedup on hash)
  pdftext.py      PyMuPDF: text-layer detection, layout-preserved text, rasterization
  extract/
    schema.py     pydantic contract: value / confidence / page / source_text
    prompt_v1.py  prompt text, versioned, frozen once shipped
    runner.py     extract(document, version) -> Extraction
    validate.py   source_text-appears-on-cited-page check
  corrections.py  record(), effective_value()
  promote.py      extraction + corrections -> policy_term / coverage / insured_item
  diff.py         term pair -> raw differences
  materiality.py  YAML rules -> classification
  premium.py      arithmetic attribution + residual
  draft.py        material + informational differences -> draft text
  web.py          FastAPI app, thin routes
  templates/      Jinja2
config/materiality.yaml
scripts/export_corrections.py
scripts/reextract.py          re-run any extractor version over every blob
evals/fixtures/*.json         redacted labels, checked in
evals/pdfs/                   real PDFs, gitignored
evals/baseline.json           per-field results from last accepted run
```

## 6. Storage architecture

**The filesystem is dumb storage. The database is the index.** Files are never organized into
per-client or per-carrier folders — folder hierarchies are why these agencies can't find
anything today, and any hierarchy chosen now would be wrong later.

**Content-addressed blobs.** On ingest: read bytes, compute sha256, store at
`blobs/<first2>/<next2>/<full-hash>.pdf`. If the hash already exists, do not write it again —
record a new `document` row pointing at the same blob. This gives free deduplication (carriers
resend documents constantly), makes the blob store immutable by construction, and eliminates
filename collisions. The original filename is metadata on the `document` row, never a path.

**Source and derived are always separate.** The blob is source of truth and never changes.
Parsed output is derived, pointing at a blob hash plus an extractor version. Improving the
extractor means re-running it over every blob and writing new `extraction` rows; old ones are
never mutated. Re-extracting the whole corpus on demand is what makes the corpus valuable.

The blob store sits behind a two-function interface (`put`, `get`) so swapping in S3 later is a
one-file change. S3 is not used now.

## 7. Data model

Model **terms**, not policies. A policy is a chain of terms; each term has effective and
expiration dates, a premium, and a set of coverages. This is the one schema mistake that can't
be fixed later, because the history to re-derive it was never captured.

Every table is insert-only. Updates are new rows.

```
client             id, display_name, created_at

policy             id, client_id, carrier_name, policy_number, line_of_business

policy_term        id, policy_id, carrier_name, policy_number, effective_date,
                   expiration_date, total_premium, source_document_id,
                   promoted_from_extraction_id, created_at

coverage           id, policy_term_id, insured_item_id NULL, coverage_code, description,
                   limit_value, limit_basis, deductible_value, premium

insured_item       id, policy_term_id, item_type, descriptor, attributes (jsonb)

document           id, blob_sha256, original_filename, page_count, has_text_layer,
                   doc_type, uploaded_at
                   -- doc_type is 'dec_page' in v0; the column exists because
                   -- endorsements and notices arrive in the same inbox later

extraction         id, document_id, extractor_version, model_id, raw_response (jsonb),
                   status, created_at

extracted_field    id, extraction_id, field_path, value, confidence,
                   source_page, source_text_span, validation_error, needs_review

correction         id, extraction_id, extracted_field_id NULL, field_path, kind,
                   extracted_value NULL, corrected_value NULL, corrected_at, note

renewal_run        id, policy_id, prior_document_id, renewal_document_id, created_at

comparison         id, renewal_run_id, prior_term_id, renewal_term_id, created_at

difference         id, comparison_id, field_path, prior_value, renewal_value,
                   materiality, rule_id

reclassification   id, difference_id, from_materiality, to_materiality, rule_id,
                   reclassified_at, note

draft              id, comparison_id, generated_text, final_text NULL, created_at,
                   edited_at NULL
                   -- generated row: final_text and edited_at NULL
                   -- edit row: both set, generated_text copied from the row it supersedes
```

### 7.1 Resolved design decisions

**`policy_term` is a promoted snapshot.** It is written once, at promotion, from a specific
`extraction_id` plus the corrections standing at that moment (`promoted_from_extraction_id`
records which). Correcting a field afterwards, or re-running the extractor, writes a *new*
`policy_term` row — never an update. Old comparisons keep pointing at the exact rows they were
computed from and stay reproducible forever. "Current term" is a query (latest promotion for a
policy and effective date), not a flag.

**Client and policy are chosen by the human at upload.** The upload form has a client selector
(existing, or a new `display_name`) and a policy selector scoped to that client. Extraction never
creates `client` or `policy` rows. This means `policy_id` is known before extraction runs, the
two documents are declared same-policy by construction, and there is no matching heuristic that
can silently weld two clients' histories together. Policy-number reformatting at renewal is
expected noise, which is exactly why it is not used as an identity key.

**`coverage.insured_item_id` is nullable.** NULL means policy-level (BI/PD, UM/UIM, medical
payments); set means the coverage belongs to that vehicle (comprehensive, collision — each with
its own deductible and premium). Without this, a $500→$1000 collision deductible on the truck and
a $500 comp deductible on the sedan flatten into indistinguishable rows. Deductible change is
material, and per-vehicle premium is the main lever for decomposing a premium move.

**`correction.extracted_field_id` is nullable and `kind` is typed.** `extraction_id` is always
set, so every correction is attributable to a version.
- `wrong_value` — field row exists, both values present
- `omission` — no field row (`extracted_field_id` NULL), `corrected_value` present. The model
  never emitted the field. Highest-signal training data and invisible in a naive UI, because
  there is nothing on screen to click.
- `hallucination` — field row exists, `corrected_value` NULL, meaning "this is not on the
  document."

**`carrier_name` and `policy_number` live on both `policy` and `policy_term`, and mean
different things.** On `policy` they are the human-chosen identity label, fixed for the whole
chain. On `policy_term` they are what was *extracted from that term's document*. Both are needed:
without the term-level copy, "carrier change" could never be detected (both terms hang off one
`policy` row, so they would share a carrier by construction) and policy-number reformatting could
never be classified as noise, because there would be nothing to compare.

**`renewal_run` holds the document pair between upload and promotion.** `comparison` cannot: it
is created at promote and frozen, and back-filling nullable term ids would mean updating a row
that must never change. Re-promoting after later corrections creates a second `comparison` under
the same run, so the run becomes the audit trail of every attempt.

### 7.2 Field path grammar

One canonical grammar, used by `extracted_field`, `correction`, `difference`, and the materiality
rules. Consistency here is what lets a rule, a correction, and an eval fixture talk about the
same thing.

```
policy.carrier_name
policy.policy_number
policy.effective_date
policy.expiration_date
policy.total_premium
coverage.<code>.limit_value                      policy-level coverage
coverage.<code>.limit_basis
coverage.<code>.deductible_value
coverage.<code>.premium
item.<key>.descriptor                            key = full VIN, else normalized year-make-model
item.<key>.attributes.<name>
item.<key>.coverage.<code>.limit_value           vehicle-level coverage
item.<key>.coverage.<code>.deductible_value
item.<key>.coverage.<code>.premium
forms.<form_number>.edition_date
```

The `policy.` prefix denotes term-scoped values read off that term's document — it resolves
against `policy_term`, not against the `policy` identity row.

Glob semantics in the rules config: `*` matches exactly one segment, `**` matches any number.
So a rule covering deductibles at both levels is `**.deductible_value`.

## 8. Extraction pipeline

Two paths, one interface, one output structure.

1. Detect a real text layer (PyMuPDF `page.get_text()` returning meaningful content).
2. **Text path:** extract text with layout preserved, send to the model with a strict JSON schema.
3. **Scanned path:** rasterize pages to PNG at ~200 DPI, send as images to the vision model with
   the same schema.

```python
def extract(document: Document, version: str) -> Extraction
```

Pure with respect to the document. No hidden state. Temperature 0.

**Every field the model returns must carry** the value, a confidence score, the page number, and
the verbatim source text it was read from. This is prompted explicitly and then validated: if the
claimed `source_text` does not appear on the cited page, the field is kept but its confidence is
forced to 0 and `validation_error` is set. An unverifiable field is evidence about the extractor,
not garbage to discard.

Fields below the confidence threshold (default 0.80, configurable) are flagged `needs_review`.
They do not reach the draft, and they block promotion until resolved.

The prompt is versioned in code (`prompt_v1.py`) and frozen once shipped. Changing the prompt
means a new version, not an edit — otherwise `extractor_version` stops meaning anything and the
eval comparison mode is worthless.

v0 targets a single carrier's personal auto dec page. No abstraction for carriers or lines that
have not been seen yet.

## 9. Corrections

Correcting a value in the review UI writes a `correction` row capturing the field path, what the
model said, what the truth was, and which extractor version produced the error. The extracted
field is never overwritten.

`effective_value(field)` resolves the current truth: the latest correction for that field path
within the extraction, else the extracted value.

This table is the long-term asset of the project. It is how a labeled evaluation set gets built
from real usage, and it is the thing a competitor starting from scratch cannot copy.

**Friction here directly destroys the asset.** Correcting a field is one click to focus, typing,
and Enter. Autosave per field — no form-wide submit button. The review screen also carries an
"add missing field" control, because omissions cannot be reported by clicking something that
isn't there.

`scripts/export_corrections.py` dumps corrections as a labeled dataset, emitting all three error
classes.

## 10. Promotion

`promote(extraction, policy_id) -> policy_term` reads the extraction plus all standing
corrections and writes a frozen snapshot: one `policy_term`, its `coverage` rows (policy-level
and vehicle-level), and its `insured_item` rows.

Correct first, promote second. Promotion is blocked while any `needs_review` field is unresolved
— each must be corrected or acknowledged. Acknowledgment is UI-side only: nothing is stored for
"the model was right," because that is not a fact worth a row in v0.

## 11. Diff engine

The diff emits **everything**. Classification labels rows; it never drops them. `noise` is a
label, not a filter, so the audit trail stays complete and it is visible how often each noise
rule fires. The UI hides noise behind a toggle.

Matching:

- policy scalars — match on `field_path`
- policy-level coverage — match on `coverage_code` where `insured_item_id IS NULL`
- vehicle-level coverage — match on (matched item, `coverage_code`)
- `insured_item` — match on full VIN; fall back to (year, make, model) when no VIN is present;
  otherwise treat as an add and a drop

Normalizers run before comparison and only canonicalize *type*: money to Decimal, dates to date,
whitespace collapsed. They never suppress a difference — sub-$1 rounding still emits a row and is
then classified `noise` by rule.

## 12. Materiality rules

The naive diff produces roughly 40 differences per renewal, of which maybe 3 matter to a client.
**The product is the filtering, not the diffing.** This judgment does not live in a prompt.

Materiality is a declarative rule set in `config/materiality.yaml`, evaluated in code, so it can
be tuned without touching the extractor and later varied per agency. Rules are an ordered list;
first match wins; the matching `rule_id` is recorded on every `difference`. Unmatched differences
fall to `default` with `rule_id: "default"`.

```yaml
version: 1
default: informational
rules:
  - id: premium_total_change
    match: { path: policy.total_premium }
    when:  { abs_delta_gte: 25, pct_delta_gte: 0.03, combine: or }
    materiality: material
  - id: deductible_change
    match: { path_glob: "**.deductible_value" }
    when:  { changed: true }
    materiality: material
  - id: form_edition
    match: { path_glob: "forms.*.edition_date" }
    materiality: noise
```

Classes:

- **material** — premium change beyond threshold, deductible change, limit change, coverage added
  or dropped, insured item added or removed, carrier change
- **informational** — address change, lienholder change, driver added
- **noise** — form edition numbers, document reordering, rounding under $1, whitespace and
  formatting differences, policy number reformatting

Reclassifying in the UI writes a `reclassification` row rather than editing the frozen
`difference`. That log is the evidence for which rules are wrong.

## 13. Premium attribution

Where the dec pages support it, the premium change is decomposed arithmetically:

- matched coverage on both sides — the per-line delta, attributed to that line
- added or dropped insured item — its full premium
- `residual = total_delta − Σ attributed`, reported signed and explicitly

No causal claims. A dec page shows the *what*, not the *why*; nothing on the page distinguishes
the carrier's rate filing from anything else, so "rate increase" is never asserted. The residual
is stated as not attributable from these documents. Residual size is also a useful signal for
extraction quality.

If total premium is missing on either side, attribution is skipped entirely and the draft says
the documents don't support it.

## 14. Draft generation

Input: material and informational differences, plus the attribution table. Output: a short,
plain-English explanation an agent can send with light editing.

- No jargon, no coverage advice, no recommendations. Explain what changed. Never tell the client
  what to do — that is licensed advice and not the model's job.
- Where a premium change cannot be explained from the documents, say so plainly rather than
  speculating.
- Under 200 words.
- Shown next to the diff table so every claim can be checked against a source.

`generated_text` is written once. Editing inserts a **new `draft` row** for the same comparison,
carrying the original `generated_text` alongside the human's `final_text`. Latest row wins. No
updates.

## 15. Web UI

```
/                      list of runs, link to new
/runs/new              client + policy selectors, two file inputs
                       POST -> ingest x2, extract x2 (blocking), redirect
/runs/{id}/review      prior | renewal fields side by side, each with confidence,
                       page, and source span; inline correction; add-missing-field
                       POST promote -> promote x2, diff, classify, draft, redirect
/comparisons/{id}      draft beside the diff table; edit draft; save
```

Extraction is a blocking 20–40s HTTP request. With one user and no job queue this is correct, and
it is a known property rather than a surprise.

## 16. Failure handling

- API error or timeout — `extraction` row with `status=failed`, error in `raw_response`, no field
  rows. Retry creates a new extraction; nothing is overwritten.
- Unparseable JSON — `status=invalid_response`, raw response kept.
- `source_text` not found on the cited page — field kept, confidence forced to 0,
  `validation_error` set, extraction `status=partial`.
- Confidence below threshold — field flagged `needs_review`; promotion blocked until corrected or
  acknowledged.
- Total premium missing — attribution skipped, stated in the draft.
- Same blob hash in both upload slots — warn and require explicit confirmation, since a document
  compared against itself is almost certainly user error.

## 17. Testing

TDD throughout. Unit tests stub the Anthropic API. Only the eval harness makes real API calls; it
is marked `@pytest.mark.eval` and excluded from the default run.

Unit coverage: blobstore dedup, diff matching (including VIN fallback and add/drop), materiality
rules over synthetic term pairs, premium arithmetic including residual, `effective_value()`
resolution across all three correction kinds, and one test asserting no extracted value reaches
log output.

Eval harness, built at 20 documents rather than 2,000 because it is cheap now and impossible to
retrofit later:

- `evals/fixtures/` — hand-labeled ground truth per test document, as JSON
- `evals/test_extraction.py` — runs the current extractor against every fixture, reporting
  per-field accuracy broken down by carrier and by field path
- `evals/baseline.json` — per-field results from the last accepted run; the test **fails loudly**
  when a previously-passing field regresses
- `scripts/compare_versions.py A B` — runs two extractor versions over the same fixtures and
  prints a per-field accuracy diff, so it is visible whether a change helped or quietly broke
  carrier #17

Real, sensitive PDFs never go in the repo. Fixture PDFs live in a gitignored directory; only the
labeled JSON is checked in, with identifying values redacted.

## 18. Privacy

These are real client documents containing names, addresses, VINs, and sometimes dates of birth.

- Nothing sensitive in logs. Log document ids and hashes, never extracted content.
- `.gitignore` covers the blob store and all real PDFs.
- `PRIVACY.md` records that documents are sent to a third-party model API, so the question can be
  answered straight when agencies ask — and they will.

## 19. Build order

Each step is shown working before the next begins.

1. Schema + migrations + content-addressed blob store. Prove ingest, dedup, retrieval.
2. Text-layer extraction for a single carrier's auto dec page, with provenance and confidence.
3. Minimal review UI: view a document, see extracted fields with sources, correct a field.
   Verify corrections are written, including omissions.
4. Eval harness with 10 hand-labeled fixtures.
5. Promotion + diff engine + materiality rules config.
6. Draft generation.
7. Scanned-document path.

## 20. Deferred, with reasons

Not gaps — decisions to revisit once there is evidence about what actually varies.

- **Multi-carrier extraction.** One carrier until there is evidence about what differs.
- **Coverage code normalization across carriers.** Same reason; a single carrier has one
  vocabulary.
- **Endorsements as first-class rows.** They attach to a term, but v0 compares dec pages only.
- **S3 blob store.** Interface is ready; the swap is a one-file change when it is needed.
- **pgvector / full-text search over the corpus.** Postgres was chosen so this needs no
  migration.
