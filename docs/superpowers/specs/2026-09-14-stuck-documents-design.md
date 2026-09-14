# Phase 3, step 11: The Stuck Documents Queue and Single-Document Review — Design

Date: 2026-09-14
Status: superseded by `2026-09-14-automatic-intake-design.md`

> Superseded before implementation. The analysis below stands and is the
> basis for three of the four `needs you` reasons in the successor spec —
> including the argument for computing state at read time, and the trap that
> a document routed but never extracted is not stuck but not-yet-processed.
> What changed is the packaging: the `/stuck` queue became a filter of the
> inbox and the single-document screen became the inbox detail view.
Scope: give a document that arrived through the pipe and cannot become a term
somewhere to be seen and somewhere to be fixed.

## Context

Step 10 wired structured extraction and promotion into `ingest_document`.
Before it, nothing that arrived through the pipe ever became a `PolicyTerm`:
`should_extract_fields` was a routing predicate with no caller, and the only
path to a term was the two-upload `RenewalRun` screen, where a human walked
every field by hand.

That change is what makes this step necessary. A document that arrives clean
and linked now promotes itself. A document that arrives *almost* clean does
not, and there is no screen anywhere that can finish it — the review screen is
per-run and pairwise, so it can only be reached through an upload pair that a
piped document does not have.

Before step 10 this cost nothing, because nothing was automatic and every term
came from a human at the review screen. Afterwards it costs a growing, silent
pile: every day the pipe runs, documents that failed the gate accumulate where
nobody can see them.

### What is already handled

`/unmatched` covers a document with no `DocumentLink` at all. It lists them
with a first-page snippet and ranked candidates, `assign()` links them, and
`evaluate()` raises an `unmatched_document` attention item so the queue is not
the only way to notice. That screen works and this step does not touch it.

### What is not

Three states are invisible. None raises an attention item, and none appears in
any queue:

| | Link | Extraction | Why it cannot promote |
|---|---|---|---|
| **B** | client, no policy | usable | `PolicyTerm.policy_id` is not nullable, and D8 forbids guessing which of a client's policies a document belongs to |
| **C** | policy | fields flagged `needs_review` | the promote gate declines rather than guessing |
| **D** | policy | produced nothing | a failed or unparseable extraction has no values to write |

State B is reachable through the existing UI: `assign()` takes
`policy_id: int | None`, and `unmatched.html` omits the hidden policy field
when a candidate has no policy id. `unmatched()` means "no `DocumentLink` row",
so a client-only link escapes that queue and lands nowhere else.

State D was worse than invisible until it was fixed during this design. A
failed provider call and an unparseable response both record the extraction
with no `ExtractedField` rows, so `unresolved_field_paths` returned empty, the
gate read that as a clean extraction, and `promote()` wrote a term whose every
value was NULL. `_latest_term` takes the newest bound term, so that empty term
became what the client page and the prep sheet showed for a policy holding a
perfectly good prior one — and `evaluate_promotion` stayed quiet, having no
effective date to compare. The gate now also asks whether the extraction is
worth promoting, which turns silent corruption into state D.

## What this step builds

1. A queue at `/stuck` listing documents in states B, C and D, computed at read
   time, with the reason on each row.
2. A single-document review screen that corrects fields, attaches a policy, and
   promotes one document.
3. The field-panel markup shared between that screen and the run review screen,
   rather than copied.

## Design

### What "stuck" means

A document is stuck when **extraction was attempted and the document still
cannot become a term**.

Every clause carries weight:

- **Attempted.** Bulk import does not extract by default — that is the whole
  point of step 10's `--skip-fields`, because an archive is thousands of
  documents and reading the coverage grid out of one is a model call. A routed
  document that was deliberately never extracted is not stuck, it is
  not-yet-processed, and the README already says extraction can be re-run later
  against whatever subset is worth it. Including those would put the entire
  imported archive in this queue and make it useless on the day it shipped.
- **Still cannot become a term.** Stated positively, as one of three named
  reasons, never as the absence of a term. A document with no term because the
  twin rule deduplicated a resend is correctly skipped, not stuck, and a
  negative definition would list it.

Formally, a document is stuck when it has at least one `Extraction`, has a
`DocumentLink` (no link is state A and belongs to `/unmatched`), has produced
no `PolicyTerm` from its latest extraction, and one of the three reasons below
holds. The reason is what puts it in the queue; having no term is a
precondition, not a reason, which is what keeps a resend deduplicated by the
twin rule out:

| Reason code | Condition | What the screen offers |
|---|---|---|
| `no_policy` | `link.policy_id is None` | attach an existing policy, or create one from this document |
| `fields_need_review` | `unresolved_field_paths()` non-empty | correct each field, or acknowledge it as extracted |
| `nothing_extracted` | `effective_values()` empty | add the fields by hand |

### Why read time, not rows

`renewal/attention/rules.py` writes event-triggered reasons as rows once at
ingest, and every one is cleared by hand: *"Nothing here auto-resolves and
nothing here acts."* Stuckness is not like that. It changes the moment she
corrects a field, and a written row would be stale immediately after the act
that fixed it.

The module already has the precedent for exactly this: the one time-based
reason, an unconfirmed date coming up soon, is computed at read time rather
than materialised, *"because it changes with the clock"*. Stuckness changes
with her, which is the same argument.

### Why its own queue

Stuck documents do not go in the attention queue, which exists for documents
that need a response — a cancellation notice, a date nobody has confirmed. Its
rules *"bias toward over-flagging"* on the reasoning that a wrong flag costs
one keystroke and a missed cancellation notice is the risk the system exists to
reduce. That trade holds only while the queue is short enough to scan.

Stuck documents are operational volume, not urgency, and their count grows with
the pipe rather than with events. Twelve badly-extracted dec pages must not be
able to bury one cancellation notice. `/stuck` is linked from the nav beside
`/unmatched`, which is the queue it most resembles.

### Components

**`renewal/stuck.py`** — a `StuckRow` dataclass and
`stuck(session, *, limit=50)`, newest first, mirroring the shape of
`unmatched()` in `renewal/resolve/service.py`. Each row carries the document,
its client and policy where known, the reason code, and the detail the reason
needs (for `fields_need_review`, the flagged paths). Two queries per row, in
line with what `/unmatched` already spends on `candidates_for`.

**`renewal/templates/_fields.html`** — one macro holding the per-extraction
panel: the verified rate, the extraction meta line, the field table with its
three correction controls, the empty state, and the add-missing-field form.
Lifted verbatim from `run_review.html`, whose panel is already entirely
run-agnostic — it reads only `side`, `extraction`, `fields`, `rates` and
`extra_fields`, all of which a single document has.

`side` is the panel heading and is the macro's one caller-supplied string: the
run screen passes "Prior" and "Renewal", the single-document screen passes the
document's class from `latest_class` ("declarations", "quote"), or "document"
when it has none. The macro takes it as a parameter rather than deriving it,
so neither caller has to know what the other calls its panels.

**`renewal/templates/run_review.html`** — calls the macro inside its existing
`sides` loop. This is a pure refactor and must change no rendered output.

**`renewal/templates/document_review.html`** — calls the macro once, above the
attach-policy and promote controls.

**`renewal/web/stuck.py`** — `GET /stuck`, `GET /documents/{id}/review`, and
`POST /documents/{id}/promote`. A new module rather than more of
`renewal/web/review.py`, which is already the joint-largest web module at 203
lines and whose docstring is about runs. The three correction endpoints
(`/fields/{id}/correct`, `/fields/{id}/reject`,
`/extractions/{id}/fields`) stay where they are and are reused untouched: they
are extraction-scoped and belong to neither screen.

### Attaching a policy

For `no_policy`, the screen offers that client's existing policies in a
dropdown, plus creating one from this document — prefilled with the extracted
`policy.carrier_name` and `policy.policy_number`, with `line_of_business`
chosen by hand because the extractor does not emit it.

Creating is offered because a client's first document otherwise dead-ends: the
policy has to exist before the term can, and sending her elsewhere to make one
and come back is a worse answer than making it here from the document that
names it.

The risk is real and is stated on the screen: a policy number typed or
reformatted differently creates a near-duplicate policy, and D8's exact-match
auto-link never fires for that chain again. The form warns when the client
already holds a policy whose number differs only in punctuation or case.

Attaching appends a new `DocumentLink` through the existing `assign()`, which
is already append-only with latest-wins. Nothing is updated in place.

### Promotion

`POST /documents/{id}/promote` takes `acknowledged` exactly as the run path
does, calls `promote()` with `kind` from `latest_class` so a quote promotes
quoted, and then `evaluate_promotion`.

**It builds no comparison and no draft.** Promoting a renewal already raises a
`renewal_received` item carrying a Compare link to the picker with both terms
preselected, and that one click is the path step 10 deliberately built. A
second, automatic path to the same place would contradict it.

### Error handling

- A document with no extraction renders "nothing to review" rather than 404 —
  she may have arrived from a queue rendered a moment ago.
- `PromotionBlocked` shows its reason, as the run path's 400 does.
- A second promote of the same document returns `None` from the existing
  per-extraction idempotence; the screen says it is already promoted and links
  to the policy.
- Promoting a document whose class does not route is allowed. She is looking at
  it and asking for it; the routing predicate exists to spend model calls
  wisely, not to overrule a human.

## Testing

`tests/test_stuck.py` — the query. Each of B, C and D appears with the right
reason. A promoted document, a never-extracted document, an unlinked document,
and a document skipped by the twin rule all do not.

`tests/test_web_document_review.py` — correct a field then promote; attach a
policy then promote; create a policy from the document then promote; the
acknowledged path; the already-promoted case; the no-extraction case.

`tests/test_web_review.py` — that `run_review.html` renders identically after
the macro extraction, by diffing the rendered output of a real run against the
same page before the change. The same discipline step 10 used at its Task 4
checkpoint, and for the same reason: a refactor that changes output is not a
refactor.

## What this deliberately does not do

- **No re-extraction button.** Extraction is a pure function of
  `(blob, extractor_version)` and re-running it is a script, not a screen
  control. A button that spends a model call per press is how an archive
  becomes an invoice.
- **No bulk promote.** Each document is promoted by someone who looked at it.
  The gate exists because the alternative is guessing at scale.
- **No merging of near-duplicate policies.** The screen warns when a created
  policy number looks like an existing one; deciding two policies are the same
  is judgment and belongs to a human with more context than this screen has.
- **`/unmatched` is untouched.** It handles a different state, it handles it
  well, and folding the two queues together is a bigger change than the gap
  justifies.
- **No attention item for a stuck document.** The queue is the place. Adding a
  row per stuck document is the crowding this design exists to avoid.
- **No attribution.** Nothing records who corrected a field or promoted a
  document. That is deferred system-wide and this step does not break the tie.
