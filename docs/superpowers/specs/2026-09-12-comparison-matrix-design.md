# Phase 3, step 10: The Comparison Matrix and the Call Prep Sheet — Design

Date: 2026-09-12
Status: approved for planning
Scope: build-order step 10 of the record layer. The comparison engine becomes
N-way, quotes become comparable, and the client page gains a printed sheet she
reads from on the phone.

## Context

The record layer landed steps 1–9. Documents arrive through one ingest pipe,
every page is text-extracted and searchable, every date lands on a calendar,
and the client overview assembles what is known about a client onto one screen.

The comparison engine did not move. `renewal/comparison.py` still takes a
`prior_term` and a `renewal_term`, `difference` still has `prior_value` and
`renewal_value`, and the only way to reach either is the two-upload
`RenewalRun` path built in v0. The record layer inverted how documents enter
the system and left comparison as the one consumer that still cannot see the
record it is sitting on.

The Phase 2 spec carried three constraints forward to this step rather than
building them:

> The comparison engine must be N-way rather than pairwise; a two-term renewal
> diff is the special case of comparing N normalized field sets.

> Normalized fields must not assume one carrier's form structure. A
> carrier-specific field goes in a typed extras map, never a promoted column.

> Carrier admitted status is a field in this phase because admitted and
> non-admitted carriers price and word coverage differently enough that
> comparing them on premium alone is misleading.

The third names what N-way is for. Comparing three consecutive terms of one
policy would not need admitted status at all — the carrier is the same in every
column. Admitted status is load-bearing when the columns are **different
carriers competing for the same risk**: an incumbent renewal against the
alternatives she has been quoted. That is the comparison this step builds.

## What moved, and what did not

Phase 2 listed **"quote comparison across wholesalers"** as a non-goal. This
spec takes the comparison and not the wholesalers.

What is built: putting quotes she already holds side by side with the renewal,
on the same normalized field paths, with the divergences classified by the same
materiality rules.

What is still not built, and still needs asking first: market access of any
kind. No submission to a wholesaler, no rater integration, no quote request, no
bind, no ACORD form, no carrier API. A quote enters this system the way every
other document does — as a PDF somebody emailed her.

The line moved from *comparing* quotes to *obtaining* them. It did not move
further than that.

## What already exists that this spec does not rebuild

- **The diff and its field-path grammar.** `renewal/diff.py` already flattens a
  term into canonical paths and normalizes type without suppressing
  differences. The N-way engine is built on `term_field_map` unchanged.
- **The materiality rules.** `config/materiality.yaml` and
  `renewal/materiality.py` classify a difference and record the rule that did
  it. The rule vocabulary is not touched.
- **Premium attribution.** `renewal/premium.py` is not edited at all. See D5.
- **Carrier resolution and admitted status.** `renewal/carriers.py` already
  resolves a free-text carrier name by exact match and reads admitted status
  per state.
- **The client overview.** `renewal/clients/overview.py` already assembles
  policies, dates, documents, messages, and attention items. The prep sheet is
  a second view over that assembly, not a second assembly.
- **The attention queue.** `renewal/attention/rules.py` already declares
  `renewal_received` and `premium_change` in `REASONS` and implements neither.
  This step implements both against the vocabulary already fixed.

## Non-goals

Not built here. Ask before adding any of them.

Market access, submissions, raters, or binding, per above. Ranking or scoring
the columns — see D2. A recommendation of any kind. Coverage-code
normalization across carriers, so that Carrier A's `COMP` and Carrier B's
`OTC` become one row; that needs evidence about what actually varies and this
step does not have it. Multi-line comparison, where a package quote is set
against three monoline policies. Proposal documents for the client. Attribution
of who built a comparison — attribution is still deferred, as it was in the
authentication spec. Any auto-built comparison; see D10.

---

## Decisions

Each of these was a real fork. Recorded with its rationale so a later reader
does not relitigate it.

### D1 — The comparison becomes a matrix, and `difference` stays the row table

Two new tables, `comparison_column` and `difference_cell`. `difference` keeps
its place as the row: one row per field path that diverges in any column, still
carrying `materiality` and `rule_id`, still the thing a `Reclassification`
points at. The cells hang off it.

Making a parallel row table instead would mean a second thing to reclassify and
two code paths through every screen. Keeping `difference` makes the Phase 2
constraint literally true in the schema — a two-term renewal diff is a matrix
with two columns, not a different object that happens to resemble one.

`difference.prior_value` and `renewal_value` stop being written. New
comparisons write cells; those two columns stay NULL. Writing both would be two
copies of one fact that can disagree, which is the reason verification rate is
computed on read and never stored.

**Rows already in her database keep rendering.** A comparison with no
`comparison_column` rows is a pairwise comparison from before this change, and
the read adapter synthesizes two columns from `comparison.prior_term_id` and
`renewal_term_id` and two cells from `prior_value` and `renewal_value`. Absence
is the marker, the same way a document with no `document_link` row is
unmatched: no status column, no backfill, and nothing rewritten to claim it was
always a matrix.

### D2 — One baseline, N−1 comparands, and nothing is ranked

Every comparison has exactly one column with `role='baseline'`. Every other
column is a comparand and is measured against it. For a renewal the baseline is
the expiring term; for remarketing it is the incumbent's renewal offer.

**No column is ever marked best, and the columns are never sorted by premium.**
They render in the order she picked them. A screen that puts three carriers
side by side and sorts the cheapest to the left has made a recommendation
whatever the words on it say, and recommending coverage is licensed activity —
the same rule that stops the draft from telling a client what to do, applied
where the pressure to break it is much higher.

The screen shows what each column says. Deciding which one to sell is her job
and it is not a job this tool is licensed to do.

### D3 — A row's materiality is the strongest across its comparands

The rules are pairwise by construction: `abs_delta_gte` and `pct_delta_gte`
need two numbers. So each comparand is classified against the baseline, and the
row records the **strongest** result across those pairs plus the `rule_id` that
produced it, ordered `material` > `informational` > `noise`.

`renewal/materiality.py` is not edited. `classify()` takes a `RawDifference`,
which is the diff's own dataclass rather than the ORM row, so the matrix hands
it one synthesized `RawDifference` per (baseline, comparand) pair and reads the
answers back. The rule engine never learns that a matrix exists.

Strongest wins because under-flagging is the dangerous direction. A row where
Carrier B matches the incumbent and Carrier C halves the liability limit is a
material row; taking the weakest, or the first, or the modal classification
would bury exactly the column that matters.

**Which cells diverge is recomputed on read.** A cell either equals the
baseline after `normalize()` or it does not; that is a pure function of two
stored values, so storing a third copy of it would only create something that
can disagree with them.

### D4 — A quote is a term with `kind='quoted'` on the incumbent's chain

`policy_term.carrier_name` already means *what was extracted from that term's
document*, and it exists precisely so a carrier change is detectable when both
terms hang off one `policy` row. A Carrier B quote carrying "Carrier B" there
is the column doing the job it was designed for. A parallel `quote_term` table
would duplicate `policy_term`, `coverage`, and `insured_item` wholesale to
express one boolean.

The risk is real and it is exactly one query. `_latest_term()` in
`renewal/clients/overview.py` is the only place in the tree that means "the
current term", and a quote read as current would print a competitor's premium
on the client page as what the client is paying. It gains `kind == 'bound'`,
and a test asserts that a quoted term never surfaces as a policy's current
term.

A quote for a line the client does not already hold has no policy to hang from.
That is out of scope: the comparison screen is reached from a policy, so there
is always an incumbent chain. A new-business quote is a different product and
would need its own identity row.

### D5 — Line-item attribution is not offered across carriers

`renewal/premium.py` is not edited. It still takes two field maps and returns
one breakdown; the matrix calls it once per comparand.

It is only called when the comparand is a term of the same policy chain as the
baseline. Across carriers the coverage codes are different vocabularies, so
almost nothing matches, almost every line falls into the residual, and a
breakdown that is 95% residual is worse than no breakdown — it looks like an
analysis. The total premium delta is still shown for every column, because
subtracting two printed totals is arithmetic and needs no shared vocabulary.

Where the line table is absent, the reason is printed in its place. This is the
same handling as a missing total premium in v0: skip it and say why, rather
than emit something that reads like an answer.

### D6 — A field the baseline has and a comparand lacks is the sharpest row

`difference_cell.value` NULL means the field is absent from that column. It
renders as **"not on this quote"** in its own style and never as an empty cell.

That absence is usually the whole reason the cheaper quote is cheaper. A blank
cell in a table of numbers reads as "nothing to report", which is the opposite
of what a dropped coverage means. The v0 diff already refuses to drop adds and
drops; this is that rule at column width.

The client template already applies this reasoning to a different value:
*"unknown is rendered as the word, never as blank: blank reads as 'nothing to
worry about', and unknown is not that."*

### D7 — Admitted status is in every column header, always, with no toggle

Per D6 of the record layer, keyed on `(carrier_id, policy.state)`. A column
whose carrier name resolves to no `carrier` row, or whose policy has no state,
reads `unknown` in words.

There is no filter and no toggle. This is the field the Phase 2 spec named as
the reason cross-carrier comparison needs care at all: an admitted carrier and
a surplus-lines carrier are not selling the same thing, and a premium
comparison that does not say so is misleading in the direction of the cheaper
column.

### D8 — A cross-carrier matrix produces no client-facing draft

The draft explains what changed at renewal. There is no wording of "here are
three carriers" that is not a recommendation, and the draft is generated text a
licensed human puts their name on.

So: a draft is generated for a matrix with exactly two columns, both `bound`,
on the same policy — a renewal. Every other matrix produces none, and the
screen says why rather than showing an empty panel. A three-term history is
also excluded, because there is no single "what changed" for it to explain.

### D9 — Carrier-specific fields land in an extras map, typed by config

Per the Phase 2 constraint, and cross-carrier comparison is where it bites:
Carrier B's dec page states things Carrier A's does not.

`policy_term_extra` holds `policy_term_id`, `field_path`, and `value`. The
grammar in `renewal/fieldpath.py` gains `extras.<key>`, and nothing else about
it changes. No extras field ever becomes a column on `policy_term`.

**The type is human-set in `config/extras.yaml`, not extracted, and not
stored.** It maps a key to `money`, `date`, `integer`, or `text`, and an
unlisted key is `text` — which compares by exact string and so cannot be wrong
in an interesting way. Three reasons it works this way:

- A model guessing whether an unfamiliar field is money or text is exactly the
  confident wrongness this project avoids elsewhere. `"1,200"` is a premium on
  one form and part of a policy number on another. Admitted status and billing
  type are human-set for the same reason.
- The type is a property of the key, not of a term, so storing it on
  `policy_term_extra` would put one fact in many rows that can disagree with
  each other and with the config. Resolved on read, there is one copy. A
  comparison already written is unaffected by a later retyping, because
  `difference` rows are frozen at build.
- It loads exactly like `config/materiality.yaml`, through a new
  `EXTRAS_CONFIG` setting, so there is no new mechanism to learn.

`normalize()` gains an optional `value_type` argument. When it is absent the
existing `MONEY_LEAVES` heuristic applies, so every current call site is
unchanged; the guess is right for a closed vocabulary and wrong for an open
one, which is why extras pass the type explicitly.

Extras fall to the rule set's `default` unless a rule names them, and a rule
can name them with the existing glob syntax (`extras.*`).

**Extras are populated by human correction, and the extractor is not
touched.** The review screen's add-missing-field control already writes an
`omission` correction against any field path, and `effective_values` already
folds corrections into what promotion reads — so she types the carrier-specific
field once and it promotes into the extras map with no prompt change at all.

That matters more than it looks. `renewal/extract/prompt_v1.py` is frozen once
shipped: teaching the extractor to emit `extras.*` means a **new extractor
version**, re-extraction across the corpus, and a fresh
`evals/baselines/<provider>__<model>__<version>` file for every provider and
model in use. None of that belongs in this step, and none of it is needed to
compare two carriers on a field a human typed in.

It also puts the decision where the correction log already lives. After she has
added the same key across a dozen documents, the log says which extras keys are
worth teaching a prompt — which is what the correction table was built to
answer.

`renewal/extract/validate.py` is not edited. Adding a production to the grammar
lets a path through `_check`'s first line; the source-text gate itself —
`_check`, `_normalize`, `validate_fields` — is untouched, as the multi-provider
spec requires.

### D10 — `renewal_received` is a fact about two rows, defined at promotion

`attention/rules.py` says today:

> `renewal_received` needs a definition of what separates a renewal dec page
> from a new-business bind, which is domain knowledge nobody has supplied — a
> guess would be a rule that is confidently wrong.

That is true of the question asked at ingest, of a document. It is not true of
the question asked at promotion, of a term:

> A promoted `bound` term on a policy that already holds a `bound` term with an
> earlier `effective_date`.

That is arithmetic over two rows, not a judgment about a PDF. The policy chain
is chosen by a human at upload and always has been, so "these are terms of the
same policy" is asserted by her rather than inferred. The reason this could not
be defined in Phase 2 is that Phase 2's rules all ran at ingest, before
anything was promoted.

**Nothing auto-builds.** The item carries a link to the comparison screen with
both terms preselected, and she clicks it. Building automatically would spend
an extraction and a draft call on every renewal that lands, including the ones
she never opens, and it would put a computed comparison in front of her that
she did not ask for — which is the thing this project does not do.

### D11 — `premium_change` fires when a comparison is built

Delta above `ATTENTION_PREMIUM_PCT` (default 10) on the baseline-to-comparand
total, on renewal comparisons only for the reason in D5. It attaches to the
renewal term's `source_document_id`, which is what `attention_item.document_id`
requires, and it is idempotent through the existing `_has_item` check.

### D12 — Stage 7 gets wired, and it costs money

`should_extract_fields` has existed since Phase 2 and **nothing calls it**.
`ingest_document` runs text, resolve, dates, classify, and attention, then
stops. Structured field extraction only ever happens on the two-upload path in
`renewal/web/runs.py`.

A quote that is stored, classified, and searchable but never extracted cannot
become a column, so this step wires the stage and adds `quote` to
`FIELD_EXTRACTION_CLASSES`.

The consequence is stated rather than discovered: **this is the first change
that spends extraction-model calls during bulk import.** An archive of ten
thousand PDFs that is mostly dec pages will now cost real money to import
against a hosted provider. Three things follow, and all three are part of this
step:

- `scripts/bulk_import.py` gains `--skip-fields`, and the stage is skipped by
  default on bulk import. Import is for getting documents in; extraction is
  re-runnable at any time by design.
- Manual upload and email intake run the stage inline, as they run every other
  stage.
- The README says which paths extract and what that costs, next to where it
  already says extraction is a pure function that can be re-run.

### D13 — The call prep sheet calls no model and computes nothing

It is an assembly of stored facts, laid out for reading aloud and printing.
Every number on it already exists in a row that something else wrote.

There is no summarization pass. A generated paragraph she reads to a client
over the phone is the one place in this system where a hallucination reaches a
client with no document, no draft, and no second look in between. The premium
bridge, the material changes, and the deadlines are all already computed and
already checkable; a model would only restate them less reliably.

### D14 — Promotion is wired into the pipeline, or D10 delivers nothing

`promote()` has exactly one caller in the tree: `renewal/web/review.py:164`,
the promote button on the two-upload review screen. Nothing in `pipeline.py`
promotes anything.

So a renewal dec page that arrives by email is stored, text-extracted,
date-extracted, classified, linked, and — after D12 — field-extracted, and then
stops. It never becomes a `PolicyTerm`. `renewal_received` is defined at
promotion (D10), so for every document that came through the pipe it would
never fire, and the attention item would only ever appear for renewals she had
already walked through the review screen by hand — where the comparison is one
click away regardless. D10 would deliver nothing.

Phase 2's pipeline diagram already specifies this stage:

> `-> promotion to PolicyTerm    only above confidence threshold`

It was never built. This step builds it, under a gate that adds no new
judgment:

- the document's latest `document_link` has a **non-NULL `policy_id`** — which
  under D8 of the record layer means an exact policy-number match, the only
  auto-link this system performs; and
- the extraction has **zero `needs_review` fields**, so `promote()` would not
  raise `PromotionBlocked` anyway.

Both conditions are already-recorded facts. Nothing new is inferred, no
threshold is softened, and `promote()` itself is not edited — it stays a pure
write of a snapshot. An extraction that fails either condition is left
unpromoted for a human, exactly as today.

A document linked to a client but not a policy is the common case for a manual
assignment, and it does not promote. That is correct: `PolicyTerm.policy_id`
is not nullable, and guessing which of a client's four policies a dec page
belongs to is the misfiling D8 exists to prevent.

---

## Architecture

### Module layout

```
renewal/
  diff.py            + diff_field_sets(baseline, comparands) -> [MatrixRow]
                       diff_terms becomes its two-set caller
  comparison.py      + build_matrix(), matrix_for()  the read adapter
                       build_comparison() becomes the two-column caller
  fieldpath.py       + extras.<key> in the grammar
  promote.py         + kind, and the extras map
  draft.py             build_prompt reads cells instead of the two columns
  pipeline.py        + the promotion stage, under the D14 gate
  extras.py          + load_types(), one small loader beside load_rules
  clients/
    prep.py          + the call prep sheet assembler
  attention/
    rules.py         + evaluate_promotion(), evaluate_comparison()
  classify/
    runner.py          FIELD_EXTRACTION_CLASSES gains quote
  web/
    comparison.py    + the column picker and the matrix POST
    clients.py       + /clients/{id}/prep
  templates/
    comparison.html    renders both shapes through matrix_for()
    compare_new.html   the column picker
    prep.html          the sheet
  static/app.css     + a print stylesheet
config/extras.yaml   + new: extras key -> type
scripts/bulk_import.py + --skip-fields
```

Not edited, and worth naming because each one is where the pressure to edit
will be: `premium.py` (D5), `materiality.py` (D3), `extract/validate.py`,
`extract/schema.py`, and `extract/prompt_v1.py` (D9).

No new package. `matrix_for()` is the seam: every consumer — the comparison
screen, the draft, the prep sheet — reads one shape, and the pairwise rows
already in the database arrive through it looking like everything else.

### The engine

```python
@dataclass(frozen=True)
class FieldSet:
    term_id: int
    values: dict[str, str | None]

@dataclass(frozen=True)
class MatrixRow:
    field_path: str
    baseline_value: str | None
    comparand_values: list[str | None]   # positional, len == N-1

def diff_field_sets(
    baseline: FieldSet, comparands: list[FieldSet]
) -> list[MatrixRow]
```

Pure, no session, no persistence. A path appears as a row when any comparand's
normalized value differs from the baseline's — including when one side is
absent, per D6. `diff_terms(session, prior, renewal)` keeps its signature and
becomes a two-set call, so every existing test of the diff keeps passing
unedited.

`MAX_COLUMNS = 5` — a baseline and four comparands. The comparison table
already has to render from 390px to 1440px, verified on 2026-08-29, and a
sixth column is where that stops being possible without hiding something. The
picker refuses more and says why.

### Reads

```python
@dataclass(frozen=True)
class Column:
    position: int
    role: str                      # baseline | comparand
    term_id: int
    carrier_name: str | None
    kind: str                      # bound | quoted
    effective_date: date | None
    admitted: str                  # admitted | non_admitted | unknown
    total_premium: str | None
    total_delta: Decimal | None    # against the baseline; None on baseline
    breakdown: PremiumBreakdown | None
    breakdown_reason: str | None   # why there is none, when there is none

@dataclass(frozen=True)
class Matrix:
    comparison: Comparison
    columns: list[Column]
    rows: list[Row]                # difference + its cells, materiality, rule
    legacy: bool                   # synthesized from prior/renewal values
    draft_eligible: bool           # D8
```

`label` is not stored. A column's heading is built on read from its term's
carrier name, kind, and effective date, because a stored label is a copy that
goes stale the moment a correction promotes a new term.

---

## Schema

Three migrations, one concern each. No migration is mixed with a feature.

### Migration 10 — the matrix

```
comparison_column   id, comparison_id, position, policy_term_id, role,
                    created_at
                    -- role in (baseline, comparand)
                    -- unique (comparison_id, position)
                    -- unique index on (comparison_id) where role = 'baseline'

difference_cell     id, difference_id, comparison_column_id, value
                    -- value NULL means the field is absent from that column
                    -- unique (difference_id, comparison_column_id)

comparison.prior_term_id     -> nullable
comparison.renewal_term_id   -> nullable
comparison.renewal_run_id    -> nullable

difference          -- unique (comparison_id, field_path)
```

Exactly one baseline per comparison is enforced by a partial unique index
rather than by application code, the same technique the authentication
migration used for uniqueness on `lower(email)`.

`prior_term_id` and `renewal_term_id` become nullable and are never written
again. They are **not dropped**: for every comparison built before this change
they are the only record of what was compared, and dropping them would delete
that.

`renewal_run_id` becomes nullable because a comparison assembled from the
record has no upload pair behind it. A `RenewalRun` is defined as "the document
pair between upload and promotion"; inventing one for a comparison that had no
upload would make the run table lie.

The unique constraint on `(comparison_id, field_path)` states something the
diff has always guaranteed. The migration asserts it holds over existing rows
before adding it, and fails loudly rather than silently dropping a duplicate.

### Migration 11 — quoted terms

```
policy_term.kind    text not null default 'bound'
                    -- kind in (bound, quoted)
```

Backfilled by the default: every term written before this change was bound, and
that is accurate rather than assumed.

### Migration 12 — the extras map

```
policy_term_extra   id, policy_term_id, field_path, value, created_at
                    -- unique (policy_term_id, field_path)
```

No `value_type` column: the type belongs to the key, not to the term, and lives
in `config/extras.yaml` per D9.

Unique per term because a term is written once at promotion and never updated;
a second value for one path would mean promotion ran twice into the same row,
which cannot happen.

---

## Components

### Building a comparison

`build_matrix(session, *, columns, rules)` takes an ordered list of
`(policy_term_id, role)` and writes the `comparison`, its `comparison_column`
rows, one `difference` per diverging path with its materiality and rule, and
the `difference_cell` rows under each.

`build_comparison(session, run_id=, prior_term=, renewal_term=, rules=)` keeps
its signature and becomes a two-column call into `build_matrix`. The promote
path in `renewal/web/review.py` is unchanged.

So after this step **every new comparison is a matrix**, including every
renewal built from the two-upload path. Only rows already in the database are
legacy, and their number stops growing on the day this ships.

### The column picker

`GET /policies/{id}/compare` lists every term on the policy — bound terms by
effective date, quoted terms grouped separately with their carrier — and lets
her mark one baseline and up to four comparands. `POST /comparisons` builds it
and redirects to `/comparisons/{id}`.

The `renewal_received` attention item links here with the two most recent bound
terms preselected, which is the one click D10 describes.

Refusals, each with its reason on screen: more than `MAX_COLUMNS` columns; no
baseline; a baseline that is not `bound`, because measuring the incumbent
against a quote inverts what every column then means; terms from different
policies, since the policy chain is what makes "the same risk" true.

### The comparison screen

`renewal/templates/comparison.html` renders `matrix_for()`. For two columns it
looks like it does today. For more it grows columns.

- Column headers carry carrier, kind, effective date, admitted status (D7), and
  total premium with its delta against the baseline.
- A quoted column is visually distinct from a bound one everywhere it appears.
- Rows are ordered material, informational, noise, then by field path, which is
  the existing `_MATERIALITY_ORDER`.
- Noise stays behind the existing toggle.
- A NULL cell renders "not on this quote" (D6).
- A cell equal to the baseline is de-emphasized; the eye should land on
  divergence.
- Reclassification is unchanged: it logs against the `difference` row and the
  screen keeps showing both the rule's answer and hers.
- The premium panel shows one breakdown per comparand where D5 allows it, and
  the reason where it does not.
- The draft panel renders when `draft_eligible`, and otherwise says why not.

Two places break on a NULL and are fixed here rather than discovered:

- `renewal/web/comparison.py:46` reads
  `session.get(PolicyTerm, comparison.prior_term_id)` to find the policy and
  client. It reads them through the baseline column instead.
- `comparison.html:5` renders a "back to run #N" link from
  `comparison.renewal_run_id`. A comparison assembled from the record has no
  run, so the link renders only when there is one, and otherwise points at the
  policy the columns belong to.

### The draft

`renewal/draft.py:build_prompt` reads `prior_value` and `renewal_value`, which
are NULL on every new comparison. It takes matrix rows instead and reads the
baseline cell and the single comparand cell. The prompt text, the system
prompt, and the 200-word rule are unchanged — this is a change of where the two
values come from, not of what the model is asked.

`generate_draft` is called only when `draft_eligible` (D8).

### The extras map

`promote()` writes a `policy_term_extra` row for every effective value whose
path starts with `extras.`. `term_field_map()` reads them back into the flat
map alongside everything else, so the diff, the rules, and the screen need no
knowledge that a path is an extra.

Types come from `config/extras.yaml`, loaded through `EXTRAS_CONFIG` the way
`config/materiality.yaml` is loaded through `MATERIALITY_CONFIG`:

```yaml
version: 1
default: text
types:
  extras.surcharge_total:      money
  extras.inspection_completed: date
  extras.driver_count:         integer
```

The engine resolves the type per path and passes it to `normalize()`. Nothing
infers a type from the value: "1,200" is money on one form and part of a policy
number on another, and guessing would make `abs_delta_gte` fire on a string
that is not a quantity.

`renewal/extract/prompt_v1.py`, `renewal/extract/schema.py`, and
`renewal/extract/validate.py` are all unchanged. Extras arrive as `omission`
corrections through the add-missing-field control that already exists, per D9.

### The call prep sheet

`GET /clients/{id}/prep`, assembled by `renewal/clients/prep.py` from the
existing `overview()` plus the latest comparison per policy. Dense, printable,
one page where it fits, and per D13 it computes nothing and calls no model.

```
ACME LANDSCAPING — call prep                        printed 2026-09-12

RENEWING WITHIN 60 DAYS
  Commercial Auto   exp 2026-10-01  (19 days)   Progressive · admitted
    premium 4,210 — was 3,900, +310 (+8.0%)
    material: COMP deductible 500 -> 1000
    material: vehicle added, 2019 F-250
    200.00 of the change is not attributable from these documents

DEADLINES                        2 unconfirmed
  2026-09-24  payment due        unconfirmed
  2026-10-01  policy expiration  confirmed

OPEN ITEMS                       1
  cancellation notice — General Liability, received 2026-09-08

RECENT MAIL                      3
```

Rules it follows, each inherited rather than invented:

- An unconfirmed date says so, everywhere, and is never presented as fact.
- A derived date shows its arithmetic and is marked computed.
- `unknown` is printed as the word.
- The unattributable residual is stated as unattributable, never as a cause.
- Nothing on the sheet is a recommendation.

The renewal block reads the latest comparison for the policy where there is
one. Where there is none, it says the terms have not been compared and links to
the picker — rather than comparing them on the spot, which would be a model
call inside a page load she opened while the phone was ringing.

The print stylesheet is the first `@media print` block in `app.css`: it drops
the topbar, navigation, and every control, and prints black on white.

### Attention

Two new entry points beside the existing `evaluate()`, both idempotent through
`_has_item`:

- `evaluate_promotion(session, term)` — writes `renewal_received` per D10,
  against the new term's `source_document_id`. Called from `promote()`'s
  callers, not from `promote()` itself, which stays a pure write of a snapshot.
  There is one such caller today, `renewal/web/review.py:164`, and D14 adds the
  second in `pipeline.py`.
- `evaluate_comparison(session, comparison)` — writes `premium_change` per D11.
  Called from `build_matrix`.

`REASONS` is unchanged. Both codes were declared in Phase 2 so the vocabulary
would be stable across exactly this change.

---

## Configuration

Two new settings on `Settings`, both with `.env.example` entries:

```
ATTENTION_PREMIUM_PCT=10        # premium_change threshold, per cent
EXTRAS_CONFIG=config/extras.yaml
```

`EXTRAS_CONFIG` mirrors `MATERIALITY_CONFIG` exactly — a path to a YAML file
loaded at use, defaulted so a fresh checkout works without an `.env` entry.

`MAX_COLUMNS` is a module constant rather than a setting. It is a property of
what fits on the screen, not of an installation, and the same reasoning already
keeps `RENEWAL_WINDOW_DAYS` and `DOCUMENT_LIMIT` out of `Settings`.

---

## Eval

No new harness. Two existing measures become load-bearing and the report says
so:

- **`quote` classification.** It now routes to structured field extraction, so
  a quote misread as `correspondence` is silently missing from the picker and a
  `correspondence` misread as `quote` silently spends an extraction call. The
  confusion matrix already reports both directions; what changes is that the
  `quote` row stops being cosmetic.
- **Extraction accuracy on non-incumbent carriers.** Every fixture today is one
  carrier's personal auto dec page. A comparison whose columns come from
  different carriers is only as good as the weakest column, and there is no
  evidence yet about the weakest column. The fixture set needs a second
  carrier before remarketing is used on a real account, and that is a
  data-collection task rather than a code task; it is named here so it is not
  discovered later.

The comparison engine itself is deterministic given its terms. It is unit
tested, not evaluated.

---

## Testing

TDD throughout. Unit tests per module; web tests through `TestClient` with the
authenticated-client helper from `tests/authhelp.py`. `conftest.py`'s `TABLES`
gains `comparison_column`, `difference_cell`, and `policy_term_extra`.

Coverage that matters:

- A two-column matrix produces exactly the differences the pairwise diff
  produced for the same terms. This is the regression that says the special
  case is really a special case.
- A comparison with `comparison_column` rows and one without both render
  through `matrix_for()` with the same shape, and the legacy one is marked
  `legacy`.
- A row where one comparand diverges materially and another does not is
  material, and records the rule that made it so (D3).
- A field present in the baseline and absent from a comparand produces a row
  with a NULL cell, and the template renders words rather than blank (D6).
- A quoted term never appears as a policy's current term on the client overview
  or the prep sheet (D4). This is the test that guards the one dangerous query.
- Attribution runs for a same-policy comparand and is skipped with a reason for
  a cross-carrier one (D5).
- A matrix containing any quoted column is not draft-eligible, and no draft
  row is written for it (D8).
- A three-column all-bound matrix is not draft-eligible either.
- The picker refuses a sixth column, a quoted baseline, and terms from two
  policies, each with its reason.
- An extras path added as an `omission` correction round-trips promotion, lands
  in the field map, and normalizes by its configured type — a money extra
  compares `1,200.00` equal to `$1,200`, a text extra does not. A key absent
  from `config/extras.yaml` is treated as text.
- An extraction with no `needs_review` fields on a document linked to a policy
  promotes through the pipeline; one with a flagged field does not; one linked
  to a client but no policy does not (D14).
- `renewal_received` fires for a promoted bound term on a policy with an
  earlier bound term, and does not fire for the first term on a policy, for a
  quoted term, or for a term whose effective date precedes the existing one.
- `premium_change` fires above the threshold on a renewal matrix and never on a
  cross-carrier one.
- Bulk import with `--skip-fields` makes no extraction call; manual upload of a
  `quote` makes one.
- The prep sheet makes no model call. Asserted with a client stub that raises.
- No new module logs document content.

### Existing tests deliberately rewritten

Recorded here rather than done quietly, because a changed test is a changed
guarantee.

- **`tests/test_comparison.py:114-116`** asserts `prior_term_id`,
  `renewal_term_id`, and `renewal_run_id` on a freshly built comparison. Under
  D1 the first two are no longer written — the same two term ids live in
  `comparison_column` — so the assertions move onto the columns. This is the
  test that would otherwise catch the change as a failure, which is exactly why
  it has to be rewritten deliberately and not adjusted until it passes.
- **`tests/test_draft.py:81-102`** builds a `Comparison` and two `Difference`
  rows with `prior_value` and `renewal_value`, then asserts over the prompt.
  `build_prompt` reads cells now, so the fixture builds columns and cells. The
  assertions about the prompt text itself do not change, because the prompt
  does not change.

`tests/test_diff.py` is **not** rewritten. It exercises `RawDifference` and
`diff_terms`, both of which keep their signatures, and it passing unedited is
the evidence that the two-term case really is the special case.

---

## Build order

Each step is shown working before the next begins.

1. Migrations 10–12, no features mixed in.
2. `diff_field_sets` and the engine, pure, no persistence.
3. `build_matrix` and `matrix_for`, with `build_comparison` rewired through it.
   `tests/test_diff.py` stays green unedited; the two rewrites named under
   Testing land in this step and nowhere else.
4. The comparison screen on the matrix, two columns only, looking like it does
   today. **Stop here and confirm nothing regressed on a real renewal.**
5. `kind`, the picker, and cross-carrier columns with admitted status.
6. The extras map, end to end: an `omission` correction on an `extras.` path
   through promotion, `config/extras.yaml`, the diff, and a rule that names it.
7. Stage 7 wired, `quote` routed, `--skip-fields` on bulk import.
8. Promotion wired into the pipeline under the D14 gate.
9. `renewal_received` and `premium_change`.
10. The call prep sheet and the print stylesheet.

Step 4 is the checkpoint. Everything before it is a refactor that must change
no output; everything after it is new behaviour. Splitting there means a
regression is attributable to one side or the other.

---

## Known limitations, stated rather than buried

- **Coverage codes are not normalized across carriers.** Carrier A's `COMP` and
  Carrier B's `OTC` are two rows, not one, and a real difference between them
  is invisible while a naming difference looks like one. This is the largest
  gap in cross-carrier comparison and it is deferred deliberately: normalizing
  on one worked example would produce a mapping that is confidently wrong on
  the next carrier. It needs several carriers' forms first.
- **Nothing checks that the quotes are for the same risk.** She can compare a
  quote written for three vehicles against a renewal covering five, and the
  matrix will show it as two dropped vehicles rather than as a quote that
  should not be on the screen. The rows are all there and they are all correct;
  reading them is her job.
- **A quote has no expiry.** Quotes go stale in days and nothing here tracks
  that. A comparison built from a two-month-old quote looks exactly like one
  built this morning.
- **Extraction accuracy on unseen carriers is unmeasured**, per the eval
  section. The weakest column sets the quality of the comparison and there is
  no evidence yet about which column that is.
- **There is no review screen for a single document.** The review screen is
  per-run and pairwise, so a document that came through the pipe with a flagged
  field cannot be corrected or promoted: it fails the D14 gate and waits. Its
  dates still reach the calendar and its text is still searchable, so it is not
  invisible — but the renewal it represents produces no `renewal_received`
  until somebody puts it through the two-upload path by hand. A per-document
  review screen is the obvious next piece of work and it is deliberately not in
  this step, which is large enough.
- **The extractor still cannot emit an extras field.** Extras arrive only by
  human correction, per D9. Teaching the extractor to find them is a new
  prompt version, a re-extraction of the corpus, and a new eval baseline per
  provider and model — real work with a real cost, and the correction log is
  what should decide when it is worth paying.
- **Attribution is still deferred.** Nothing records who built a comparison,
  who reclassified a row, or who printed a prep sheet — consistent with the
  authentication spec, and still wrong the day a second person uses this.
- **The index page still lists runs.** It has been the wrong front door since
  the record layer shipped, and a comparison built from the record does not
  appear on it at all. Fixing it is not this step's job; it is noted so the
  absence is known rather than surprising.
