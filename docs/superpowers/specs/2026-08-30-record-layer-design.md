# Phase 2: The Record Layer — Design

Date: 2026-08-30
Status: approved for planning
Scope: build-order steps 1–9. Step 10 (comparison refactor, call prep sheet) gets its own spec.

## Context

The working slice proved the extraction path: content-addressed blobs, LLM
extraction into a neutral field set with provenance and confidence, a
validation pass that rejects source text absent from the cited page, a review
screen where every human fix writes a `Correction`, promotion into
`PolicyTerm`, a YAML-rule diff engine with materiality classification and
premium attribution, and a draft generator.

The only way a document enters that slice is by being uploaded into a
comparison run. This phase inverts that: documents enter through an ingest
pipe, everything is stored and searchable, every date is extracted and lands
on a calendar, and renewal comparison becomes one consumer of the record.

```
intake (email fwd, bulk import, manual upload)
    -> blob store + document row + raw text        every document, always
    -> client/entity resolution                    every document, human-assisted
    -> date extraction                             every document, always
    -> structured field extraction                 only documents worth it
    -> promotion to PolicyTerm                     only above confidence threshold

consumers: calendar / search / client overview / attention queue / comparison
```

**The invariant this design preserves everywhere:** storage, text extraction,
date extraction, and search have no judgment in them and cannot be silently
wrong in a harmful way. Only some documents get structured field extraction,
and only high-confidence extractions get promoted. Search and calendar never
depend on field-extraction accuracy, and nothing in this document couples them.

## What already exists that this spec does not rebuild

- **The eval harness.** `evals/accuracy.py` and `evals/test_extraction.py`
  already do per-carrier and per-field-path accuracy, baseline regression
  gating via `regressions()`, and A/B across extractor versions via
  `baseline_path()` keyed on provider, model, and version. Build-order step 1
  is an extension, not a build.
- **Content-addressed blob storage**, `renewal/blobstore.py`.
- **Provenance validation**, `renewal/extract/validate.py`.
- **The multi-provider model client**, `renewal/providers.py`.

## Non-goals

Not built this phase. Ask before adding any of them.

Gmail/Outlook OAuth or any mailbox API. WeChat integration of any kind.
Outbound email or messaging. Automated client follow-up or reminder sequences.
Reading sent mail or detecting that a reply happened. Anything touching
payments or fund movement. ACORD form generation, filling, or distribution —
no implementation, no scaffold, no stub. Quote comparison across wholesalers.
Auth, accounts, multi-tenancy, billing. Automatic actions of any kind:
everything is a draft or a suggestion a human confirms.

## Assumption

Designed for an archive in the low tens of thousands of PDFs. At 100k+ the
stage-pass bulk import in §3 would need a work queue instead.

---

## Decisions

Each of these was a real fork. Recorded with its rationale so a later reader
does not relitigate it.

### D1 — Mutable state becomes event tables; the insert-only invariant holds

`models.py` states: *"Every table here is insert-only. Application code never
issues UPDATE or DELETE."* Date confirmation and attention resolution would
have broken that.

`document_date` and `attention_item` are immutable facts. Human judgment lands
in `date_event` and `attention_event`. Current status is the latest event,
`unconfirmed` / `open` when there is none. This matches how `Correction` and
`Reclassification` already work, and the judgment log is the training data for
tuning date-extraction precision — after she dismisses two hundred spurious
dates, the log is what tells us which `date_type` the extractor gets wrong.

The same rule applies to human-set attributes: `carrier_admitted_status` and
`policy_billing_type` are append-only rows where the latest wins, not columns
that get UPDATEd.

### D2 — OCR is local Tesseract; the model never sees a scan for text

`pytesseract` over a rasterized page. Nothing leaves the host, free at any
archive size, and `extraction_method` records `pymupdf` or `ocr_tesseract` so
quality can be measured per method.

Tesseract is weaker than a vision model on faxed and skewed pages. Accepted:
this path feeds search recall and date-shaped strings, not structured field
accuracy. Structured extraction keeps its existing vision path unchanged.

### D3 — Dates: a free regex floor plus a bounded LLM pass, both stored

The regex pass sweeps every page for date-shaped literals. It is free,
exhaustive, and its provenance is perfect by construction — the matched string
*is* the `source_text`, so validation can never reject it.

The LLM pass reads the first N pages, assigns `date_type` to what the regex
found, and reports dates the regex cannot see: prose deadlines such as *"within
30 days of the date of this notice"*. On a cancellation notice that is the most
important date in the document and it contains no digits.

Both passes' rows are stored, distinguished by `pass` and `extractor_version`,
so their recall can be measured separately.

### D4 — Computed dates carry their arithmetic and are never shown as read

A relative deadline resolves to a real `date_value`, but that value was never
printed on the document. `is_derived`, `anchor_date`, and `anchor_source_text`
record how it was reached; the UI shows the arithmetic; a derived date can
never be auto-confirmed.

This is the one place in the design where the system calculates rather than
reads. It is flagged in the UI as such.

### D5 — Dates and attention items do not store `client_id`

Both derive it through the document's latest `document_link`. Re-assigning a
misfiled document instantly moves every date on it to the right client, with no
backfill and no stale rows. Cost is one join on the hot calendar query.

### D6 — Admitted status is per state

The same carrier can be admitted in one state and surplus-lines in another, so
a single boolean on `carrier` is wrong the day she writes in a second state.
`carrier_admitted_status` is keyed on `(carrier_id, state)`.

Never inferred from a document. Human-set only.

### D7 — `billing_type` lives on both `Policy` and `PolicyTerm`

`policy_billing_type` is her authoritative human-set value. `policy_term
.billing_type` is what a dec page said, populated by the structured extractor
where the form states it. The overview shows hers and flags a term that
disagrees, because a disagreement means billing changed at renewal — itself
worth seeing. `unknown` is the default and is preferable to a guess.

### D8 — Auto-linking requires an exact policy-number match, nothing less

Name similarity alone never auto-links. That is exactly where misfiling
happens, and a cancellation notice filed under the wrong client is the worst
outcome this system can produce. Everything else queues for one-click human
assignment.

### D9 — Blobs get app-level AES-GCM with the key in the environment

The plaintext is hashed, so content addressing and dedup are unchanged; the
bytes on disk are sealed. Defends against a stolen backup, a copied `blobs/`
directory, a decommissioned disk. **Does not defend against a compromised
host** — the key sits next to the data. PRIVACY.md states that limit plainly.

### D10 — The `.ics` URL's token is the credential

There is no auth in this phase and calendar apps cannot log in. The feed lives
at `/calendar/<32-byte urlsafe token>.ics`, and the token is regenerable from
the UI so a leaked link can be killed. The token will sync to her phone with
her calendar config; if it leaks, the whole book's deadline map leaks with it.
Documented, not hidden.

### D11 — The mail provider is deferred behind an interface

`InboundProvider` plus a local file-drop implementation ships now. Postmark,
CloudMailin, or SES gets chosen when the domain is registered. Nothing in the
record layer depends on which wins.

### D12 — One `agency` row, no tenancy

`agency` exists so `agency_id` has something to point at and so the ics token
and intake address have a home she can rotate from the UI. One row, seeded by
migration. No auth, no login, no tenancy.

---

## Architecture

### Module layout

```
renewal/
  crypto.py              AES-GCM seal/unseal for the blob store
  pipeline.py            the single ingest entry point
  text/
    ocr.py               tesseract
    store.py             per-page text rows
  dates/
    regex_pass.py
    llm_pass.py
    prompt_v1.py
    service.py           orchestration, validation, insert
  resolve/
    candidates.py        identifier extraction from page text
    matching.py          scoring and ranking
    service.py           auto-link or leave unmatched
  classify/
    prompt_v1.py
    runner.py
  search/
    query.py
  attention/
    rules.py
  calendarview/
    agenda.py
    ics.py
  mail/
    provider.py          InboundProvider protocol
    filedrop.py          local implementation
    parse.py             MIME -> documents
  web/
    __init__.py          create_app, wiring
    runs.py review.py comparison.py        existing routes, moved verbatim
    calendar.py search.py clients.py unmatched.py attention.py
    settings.py            agency settings: ics token, intake address,
                           carrier admitted status, policy billing type
```

`renewal/web.py` is 425 lines and this phase would roughly triple it. Splitting
it into a router package is the targeted improvement this work justifies. The
existing routes move verbatim in a commit that changes no behavior. Every other
existing flat module stays where it is; nothing in the working slice moves.

### The pipeline

`pipeline.ingest_document()` is the one entry point. Bulk import, manual
upload, and email intake all call it.

```
1 blob       store.put, now sealed                       always
2 document   row + source + agency_id                    always
3 text       pymupdf per page, tesseract where absent    always
4 resolve    -> document_link, or leave unmatched        always
5 dates      regex pass + LLM pass                       always
6 classify   cheap LLM over page 1 text                  always
7 fields     only when doc_class in (declarations, endorsement)
8 attention  rules over the results of 4, 5, 6
```

Stages 3 through 8 are independently re-runnable and version-keyed. A stage
skips a document that already has output at the current version, so every
stage is idempotent.

`document.source` is one of `bulk_import`, `manual_upload`, `email_attachment`,
`email_body`.

**Execution model.** No job queue and no daemon. `scripts/bulk_import.py` runs
one stage at a time across the whole tree — all text, then all resolution, then
all dates, and so on. Resumable by construction, safe to Ctrl-C, safe to
re-run. Manual upload runs the full pipeline inline; it is a handful of model
calls and keeps the demo path as fast as it is today. The email webhook stores
the MIME, returns 200, and processes in a FastAPI background task.

---

## Schema

Six migrations, one concern each. No migration is mixed with a feature.

### Migration 1 — agency and carrier

```
agency                   id, slug, display_name, ics_token,
                         intake_address, created_at
                         -- seeded with one row: slug 'default'

carrier                  id, display_name, created_at
carrier_alias            id, carrier_id, alias, created_at
carrier_admitted_status  id, carrier_id, state, status, set_by, set_at
                         -- status in (admitted, non_admitted, unknown)
                         -- append-only; latest per (carrier_id, state) wins

policy.state             char(2), nullable          -- which state's admitted
                                                    -- status applies
```

`carrier_alias` exists because `carrier_name` is free text on both `Policy` and
`PolicyTerm` today, and "Progressive" and "Progressive Casualty Ins Co" must
resolve to one carrier. Resolution is by normalized exact match against
`display_name` or an alias; no fuzzy carrier matching, and an unrecognized name
creates nothing automatically — it surfaces in the unmatched-carrier list for
her to alias or create.

`policy.state` nullable means admitted status renders as *"unknown — no state
on policy"* rather than guessing.

### Migration 2 — billing type

```
policy_billing_type      id, policy_id, billing_type, set_by, set_at
                         -- billing_type in (direct_bill, agency_bill, unknown)
                         -- append-only; latest per policy_id wins

policy_term.billing_type text, nullable            -- extracted, unknown-safe
```

For direct-bill policies, `payment_due` dates are usually not knowable from the
documents she receives. Absence of a payment date on a direct-bill policy is
not a recall failure and the eval harness must not score it as one (§ Eval).

### Migration 3 — documents and text

```
document  + source            text, not null, default 'manual_upload'
          + agency_id         fk agency

document_text            id, document_id, page_number, text,
                         extraction_method, extractor_version, created_at,
                         tsv tsvector GENERATED ALWAYS AS
                             (to_tsvector('english', text)) STORED
                         unique (document_id, page_number, extractor_version)
                         GIN index on tsv

document_classification  id, document_id, doc_class, confidence,
                         classifier_version, model_id, created_at
                         -- latest by created_at wins
```

`extraction_method` is `pymupdf` or `ocr_tesseract`.

`doc_class` is one of `declarations`, `endorsement`, `cancellation_notice`,
`non_renewal_notice`, `invoice`, `id_card`, `loss_run`, `inspection_report`,
`quote`, `correspondence`, `unknown`.

Classification is a separate table rather than a column so re-classification
inserts rather than updates, consistent with D1. The existing
`document.doc_type` column is untouched; it is the v0 marker the current
extractor reads and is unrelated to `doc_class`.

`CREATE EXTENSION IF NOT EXISTS pg_trgm` lands in this migration; both search
and client matching need it.

### Migration 4 — client resolution

```
document_link            id, document_id, client_id, policy_id, method,
                         confidence, candidates jsonb, created_at
                         -- method in (auto, manual)
                         -- append-only; latest per document_id wins
```

A document with no `document_link` row is unmatched. There is no status column
and no queue table: the queue is the set of documents without a link.

A manual assignment inserts a `manual` row carrying the ranked candidate list
that was shown. That row plus the superseded `auto` row it replaced is the
Correction-equivalent record — what was offered, and what was right.

### Migration 5 — dates and the calendar

```
document_date            id, document_id, date_value, date_type, source_page,
                         source_text, confidence, extractor_version, pass,
                         is_derived, anchor_date, anchor_source_text,
                         created_at
                         -- pass in (regex, llm)
                         index on (date_value), index on (document_id)

date_event               id, document_date_id, action, note, actor, created_at
                         -- action in (confirmed, dismissed, superseded)
                         -- 'superseded' is written when a re-extraction at a
                         -- newer version replaces a row she had already acted
                         -- on, so her judgment is preserved rather than
                         -- silently attached to a stale row

manual_date              id, agency_id, client_id, title, date_value,
                         date_type, notes, created_by, created_at
manual_date_event        id, manual_date_id, action, note, actor, created_at
```

`date_type` is one of `policy_effective`, `policy_expiration`, `renewal_due`,
`cancellation_effective`, `non_renewal_effective`, `payment_due`,
`inspection_deadline`, `remediation_deadline`, `audit_date`, `other`.

No `client_id` or `policy_id` on `document_date`, per D5.

`actor`, `set_by`, `created_by`, and `confirmed_by`-equivalents are text labels
defaulting to `'human'`. There is no auth to populate them properly, and a
visible placeholder is better than something that looks like an identity.

### Migration 6 — inbound mail and the attention queue

```
inbound_message          id, agency_id, message_id, from_address, to_address,
                         subject, received_at, raw_mime_blob_sha256,
                         body_text, processing_status, created_at
                         unique (agency_id, message_id)
                         -- processing_status in (received, processed,
                         --    quarantined, duplicate, failed)

attention_item           id, document_id, reason_code, reason_text,
                         due_date, created_at
attention_event          id, attention_item_id, action, note, actor, created_at
                         -- action in (done, dismissed)

document  + inbound_message_id  fk inbound_message, nullable
```

`document.inbound_message_id` is added here rather than in migration 3 because
its target table does not exist until this migration.

`processing_status='failed'` records a message whose MIME could not be parsed.
The raw blob is kept so the failure can be diagnosed and the message re-processed
after a parser fix; it is never discarded.

---

## Components

### Text extraction (§2)

For each page, `page.get_text("text")` via PyMuPDF. If the normalized character
count is at least `MIN_CHARS_FOR_TEXT_LAYER` (100, the existing constant), the
method is `pymupdf`. Otherwise the page is rasterized at 300 dpi and run
through Tesseract, method `ocr_tesseract`. Per page, not per document: a PDF
with a scanned endorsement stapled to a digital dec page gets both methods and
the row records which.

An empty page still gets a row with empty text, so the skip logic can tell
"processed, nothing there" from "not processed".

Tesseract failure logs and writes no row; the stage is re-runnable and a
re-run retries. It never fails the whole import.

`document.has_text_layer` keeps its current document-level meaning; the
existing structured extractor reads it and is not changed by this phase.

New dependencies: `pytesseract`. Tesseract itself is a system package and is
documented as such in the README.

### Date extraction (§5)

**Regex pass** — `dates/regex_pass.py`, version `dates-regex-v1`, free, every
page. Recognizes `MM/DD/YYYY`, `M/D/YY`, `YYYY-MM-DD`, `June 1, 2026`,
`1 June 2026`, `Jun 1 2026`. Slash-separated dates are read month-first; these
are US insurance documents. `source_text` is the matched literal exactly as it
appears on the page, so validation is a tautology and can never reject it.

`date_type` comes from keyword rules over an 80-character window around the
match ("Expiration Date", "Cancellation Effective", "Audit"). No keyword hit
means `other`. Confidence is a fixed heuristic: 0.5 with a type keyword, 0.3
for `other`. These numbers are heuristics, not probabilities, and are labeled
as such in the code.

**LLM pass** — `dates/llm_pass.py`, version `dates-llm-v1`, first
`DATE_PAGES` pages (default 3; deadlines live near the front and cancellation
notices are one or two pages). Returns `date_value` in ISO form, `date_type`,
`source_page`, `source_text`, `confidence`, `is_derived`, `anchor_date`,
`anchor_source_text`.

**Validation** reuses the normalization in `extract/validate.py`. If
`source_text` is not present on the cited page, **the date is rejected and not
stored.** This deliberately diverges from field extraction, which stores an
unverifiable field at zero confidence: a field at zero confidence is evidence
about the extractor, but a date that renders on a calendar is a claim, and an
unverifiable claim on a calendar is the failure mode this product exists to
prevent. Rejections are counted and reported by the eval harness as recall
loss.

**Derived dates.** A derived date is stored with `is_derived` true. When
`anchor_source_text` is verifiable on the cited page, the UI shows the
arithmetic — *"computed — notice dated 06/01 + 30 days"*. When it is not, the
row is still stored but renders as *"computed — anchor not verified"* and
carries a stronger warning. A derived date can never be auto-confirmed.

**Both passes are stored.** The calendar collapses rows sharing
`(document_id, date_value, date_type)` into one entry for display, preferring
the LLM row, and shows which passes produced it. Storage keeps them apart so
their recall is measurable independently.

Bias is toward over-extraction throughout. A spurious date she dismisses costs
two seconds; a missed cancellation deadline is the entire risk of this product.

### Classification (§3)

`classify/runner.py`, model from `CLASSIFICATION_MODEL` (default
`claude-haiku-4-5-20251001`), input is the page-1 text from `document_text` —
not the PDF. Returns a label and a confidence. `unknown` is always an
acceptable answer and is preferred to a confident wrong guess.

Classification decides routing and display only. It never gates storage,
search, or date extraction. Only `declarations` and `endorsement` route to
structured field extraction. `quote` is classified and stored; nothing consumes
it this phase.

### Client and policy resolution (§4)

`resolve/candidates.py` pulls identifiers from page-1 text: policy numbers
(alphanumeric-with-dashes near a "Policy Number" label), named insured (the
text following a "Named Insured" or "Insured" label up to the line break), and
address (following lines carrying a state-and-ZIP pattern).

`resolve/matching.py` scores against existing records. An exact normalized
policy-number match to a `Policy` scores 1.0. Named-insured similarity uses
`pg_trgm` against `client.display_name`; address token overlap contributes a
small boost. It returns the top five with scores — never a single silent
answer.

`resolve/service.py` auto-links only when exactly one candidate scores 1.0,
i.e. exactly one exact policy-number match (D8). Everything else is left
unlinked and appears in the unmatched queue.

The unmatched queue shows the ranked candidates with scores, assigns in one
click, and can create a new client inline. Both paths insert a `manual`
`document_link` carrying the candidate list that was shown.

### Calendar (§5)

Agenda is the default view; month is secondary. Entries union `document_date`
(status from its latest `date_event`) with `manual_date`.

**Escalation.** An entry is pinned to the top of the agenda and visually
escalated when *either* the document's latest classification is
`cancellation_notice` or `non_renewal_notice`, *or* the `date_type` is
`cancellation_effective` or `non_renewal_effective`. The second condition means
a misclassified notice still escalates — display never depends on
classification being right.

Unconfirmed dates render visually distinct from confirmed ones and are never
presented as fact. Confirmation is one click; dismissal is one keystroke.
Filters: client, date type, confirmed/unconfirmed.

`.ics` export is read-only and subscribe-only at `/calendar/<token>.ics` per
D10, with a regenerate control on the agency settings page. Nothing is ever
written into her calendar and there is no two-way sync.

Keystroke dismissal needs `renewal/static/app.js`. Related bug fixed in the
same commit: `pyproject.toml` declares `package-data = ["static/*.js"]` while
the only static file is `app.css`, so the installed wheel currently ships no
CSS. The glob becomes `static/*`.

### Search (§6)

One box. `websearch_to_tsquery` against `document_text.tsv`, unioned with
`pg_trgm` matches on `client.display_name` and `carrier.display_name` and an
exact-prefix match on `policy.policy_number`. Results carry client, document
class, upload date, carrier, and a `ts_headline` snippet with the term
highlighted. Ordered newest-first by `document.uploaded_at`, not by rank.
Filters: client, document class, upload date range.

Indexes: GIN on `document_text.tsv`; GIN trigram on `client.display_name` and
`carrier.display_name`; btree on `policy.policy_number` and on
`document.uploaded_at DESC`.

The 300ms-at-10k-documents target is a test, not an aspiration: a seeded
performance test under a `perf` pytest marker, excluded from the default run
and executed explicitly.

### Client overview (§7)

`/clients/{id}`, dense, no pagination above the fold, no clicking to reveal
basics. Built for scanning while she is on the phone.

Policies with current term dates, carrier, premium, admitted status for that
policy's state, and billing type — with a flag when the extracted
`policy_term.billing_type` disagrees with her set value. Upcoming dates with
unconfirmed ones marked. A renewal countdown for anything within 60 days. Full
document history, newest first, one click to the PDF. Recent inbound messages.
Open attention items for this client.

### Inbound email intake (§8)

`mail/provider.py` defines the protocol: `verify(headers, body) -> bool` and
`parse(headers, body) -> InboundEmail`. `mail/filedrop.py` reads `.eml` files
from a directory for local development and tests. The hosted provider is chosen
when the domain is registered (D11) and the choice is documented in a comment
at the interface.

Routing is by recipient: `to_address` must match an `agency.intake_address`.
No match means the message is stored with `processing_status='quarantined'` and
no documents are created — never silently dropped. Dedupe is on
`unique(agency_id, message_id)`; a duplicate is recorded as `duplicate` and
does no work. Attachments dedupe naturally through content addressing.

**The message body is stored as a document**, not only its attachments. The
carrier's explanation is frequently in the body while the attachment is a bare
form, and deadlines are very often stated in prose in the body. The body
document's blob is the raw MIME blob it shares with `inbound_message`, and its
`document_text` page 1 is the extracted body text, so the body flows through
the same search index and the same date extraction as everything else.

`BlobStore.path_for` currently hardcodes a `.pdf` suffix. It gains an extension
parameter defaulting to `pdf`, so raw MIME can be stored as `.eml` without
touching existing blobs.

### Attention queue (§9)

Deliberately not a task manager: a list of documents that appear to need a
human response, with a suggested reason.

Event-triggered reasons materialize as `attention_item` rows at ingest:
`cancellation_notice`, `non_renewal_notice`, `renewal_received` (a
`declarations` document linking to a policy that already has a term),
`premium_change`, and `unmatched_document`.

`premium_change` (delta above `ATTENTION_PREMIUM_PCT`, default 10) fires when a
comparison is built, not at ingest — there is no comparison during bulk import.
Until the step-10 refactor that means the existing manual two-PDF path only.

`unconfirmed_date_within_14_days` is computed live in the queue view rather
than materialized, because it changes with the clock and materializing it would
need a daemon. Dismissing one writes a `date_event` dismissal, which is the
correct action for it anyway.

An item with a due date renders on the calendar; one without renders only in
the queue. She resolves items in one click. Nothing ever auto-resolves and
nothing ever auto-acts — we cannot detect that she replied without reading sent
mail, which needs OAuth we are deliberately not doing. Rules bias toward
over-flagging and dismissal is a single keystroke.

---

## Security and privacy (§12)

`crypto.py` provides `seal(plaintext, key)` returning a `RNB1` magic header, a
12-byte nonce, and AES-GCM ciphertext, plus `unseal`. `BlobStore` takes an
optional key: `put` hashes the plaintext, so content addressing and dedup are
unchanged, and writes the sealed bytes; `get` unseals. The magic header lets
`scripts/encrypt_blobs.py` walk an existing store and seal what is not yet
sealed, idempotently.

The key is `BLOB_ENCRYPTION_KEY`, base64 of 32 bytes. **When it is absent,
blobs are stored unencrypted and the app logs a warning at startup.** That
keeps local development and the eval harness working without key management,
and PRIVACY.md states it plainly rather than implying encryption is
unconditional.

New dependency: `cryptography`.

Logging never carries document content — ids, hashes, counts, and statuses
only, as the existing `ingest.py` and `extract/runner.py` already do. The new
modules follow the same rule and it is asserted in tests.

`.gitignore` gains raw MIME and import staging alongside the existing `blobs/`
and `evals/pdfs/`.

`scripts/redact.py` builds eval fixtures from real documents: it reads a real
PDF's text, substitutes names, addresses, VINs, and policy numbers with
synthetic values, and re-emits through the existing `tests/pdfmaker.py`.
Redacting a PDF in place is unreliable; regenerating one is not.

PRIVACY.md is updated to cover what is stored, where, which third-party model
APIs see document content, and retention — including that OCR is local and no
scan is transmitted for text, that the blob key sits beside the data, and that
the `.ics` token is a bearer credential.

---

## Eval harness (§13)

Extends the existing harness rather than replacing it. Fixtures gain expected
`doc_class`, expected client match, and an expected date list.

- **Dates.** Precision and recall are reported separately. Only recall
  regression fails the build; a missed date is the failure mode with real
  consequences. Validation rejections are counted and reported as recall loss
  so a strict validator cannot quietly hide behind a good precision number.
  `payment_due` on a direct-bill policy is excluded from recall scoring: those
  dates are genuinely not in the documents she receives, and scoring them would
  chase a field that is not there.
- **Classification.** Accuracy plus a confusion matrix. `unknown` is scored as
  a declined answer, not a wrong one — scoring it as wrong would punish exactly
  the behavior we want.
- **Client matching.** Top-1 accuracy and recall@5, so a wrong auto-link and a
  bad candidate list are distinguishable failures.

A/B across extractor versions keeps working through the existing
`baseline_path()` keying, which already separates provider, model, and version.

---

## Testing

TDD throughout. Unit tests per module; web tests through `TestClient` following
the existing `tests/test_web_*.py` pattern; the `session` fixture for
rolling-back tests and `clean_db` for committing ones. `conftest.py`'s `TABLES`
tuple gains every new table so the web tests keep truncating cleanly.

Specific coverage that matters:

- A date whose `source_text` is absent from the cited page is not stored.
- A derived date with an unverifiable anchor is stored and flagged, not dropped.
- Re-assigning a document's link moves its dates to the new client with no
  backfill (D5).
- Classification returning `unknown` does not prevent storage, text
  extraction, date extraction, or search.
- A misclassified cancellation notice still escalates on the agenda via its
  `date_type` (§Calendar).
- Auto-link fires on exact policy number and on nothing else (D8).
- A duplicate `Message-ID` creates no second document.
- Mail to an unknown intake address is quarantined, not dropped.
- Sealed and unsealed blobs both read back; hashes are stable across sealing.
- No new module logs document content.

---

## Build order

Each step is shown working before the next begins.

1. Eval harness extension — dates, classification, client matching.
2. Schema migrations, six of them, no features mixed in.
3. Bulk import + text extraction.
4. Client resolution + unmatched queue.
5. **Date extraction + calendar. Stop here.** This is the first thing that goes
   in front of her, and it is tested on her real archive before anything
   further is built.
6. Search.
7. Client overview page.
8. Inbound email intake.
9. Classification + attention queue.

The `web.py` router split lands as a behavior-free commit before step 5, which
is the first step that adds a substantial block of new routes. Splitting after
it would mean moving code twice.

## Phase 3 constraints carried, not built

The comparison engine must be N-way rather than pairwise; a two-term renewal
diff is the special case of comparing N normalized field sets. That refactor is
step 10 and gets its own spec, but nothing in this phase may assume pairwise.

Normalized fields must not assume one carrier's form structure. A
carrier-specific field goes in a typed extras map, never a promoted column.

Carrier admitted status is a field in this phase (D6) because admitted and
non-admitted carriers price and word coverage differently enough that comparing
them on premium alone is misleading.

## Known limitations, stated rather than buried

- The blob encryption key sits next to the data. This defends against stolen
  backups and disks and nothing more.
- The `.ics` token is a bearer credential that will sync to her phone. A leak
  exposes the whole book's deadline map until she regenerates it.
- Tesseract is weaker than a vision model on faxed and skewed pages, so search
  recall on the worst scans will be imperfect. Structured extraction is
  unaffected; it keeps its existing vision path.
- Derived dates are arithmetic the system performed, not text it read. They are
  flagged everywhere they appear and can never be auto-confirmed.
- `actor` fields are text placeholders because there is no auth.
