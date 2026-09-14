# Phase 3, step 11: Automatic Intake and the Inbox — Design

Date: 2026-09-14
Status: approved for planning
Supersedes: `2026-09-14-stuck-documents-design.md` (its reasoning survives; its
screens move into the inbox — see "Relationship to the stuck-documents spec")
Scope: delete the renewal-run flow, run intake in the background, and put every
document that arrives on one screen with its state and its fix.

## Context

The goal is a person who is not technical opening this once a day, seeing that
the work happened, and touching only what genuinely needs a human.

Step 10 wired extraction and promotion into `ingest_document`, so a document
that arrives clean and linked already becomes a `PolicyTerm` with no help. What
it did not do is remove the older path, make the automatic path finish the job,
or give anyone a way to see that any of it happened.

### `/runs` is not the automatic path with a UI on it

`POST /runs` calls `ingest_pdf` and `extract` directly (`renewal/web/runs.py:90`,
`:93` and `:104`). It never calls `ingest_document`, so a document uploaded there
skips resolution (auto-linking to a policy), date extraction onto the calendar,
classification, promotion, and attention rules.

That is why the run flow needs a human to pick the policy and press promote: it
never ran the stages that would have done those things. It is a v0 path that
does strictly less than the pipe beside it, and deleting it is a gain in
capability rather than a trade.

It survives today only because it owns two things nothing else can reach:
`generate_draft` is called from exactly one place (`renewal/web/review.py:198`),
and the three field-correction endpoints are rendered by exactly one template
(`run_review.html`). A document that arrives by email with one flagged field
cannot be corrected anywhere in the application.

### The notification is on the wrong side of the click

`premium_change` — the item that says the premium moved — is raised by
`evaluate_comparison`, which runs inside `build_matrix`
(`renewal/comparison.py:250`). The pipeline never calls `build_matrix`; it
promotes a term and stops.

So the alert that exists to prompt her to compare only comes into existence
after she has already compared. If she never clicks, she is never told. Step
10 described that click as "the one click D10 describes" and did not notice the
notification sat behind it.

### Nothing shows that any of it happened

There is no screen anywhere that lists documents by what became of them. The
index lists runs, which after this change is a list of one obsolete thing.
`/unmatched` shows only documents with no `DocumentLink` at all. A document
that was linked, extracted, and promoted leaves no visible trace except a term
on a client page she would have to know to visit.

## Decisions

1. **Email is the primary intake; a one-file drop box is the backup.** Both go
   through the same code path. The two-upload form, the policy picker, the
   prior/renewal slots and the confirm-same checkbox are all deleted.
2. **Intake runs in the background, in-process.** The request stores the file
   and answers immediately. No new services; a document caught by a restart is
   shown as stalled rather than hidden.
3. **Every gate stays exactly as strict.** Automation means the clean case
   needs no touches, not that the ambiguous case gets guessed at. D8's
   exact-policy-number rule is unchanged, and the promote gate is unchanged.
   What changes is that each exception is visible with its fix attached.
4. **One inbox is the front door.** `/unmatched` and the stuck states become
   filters of it rather than separate destinations.
5. **A renewal comparison builds itself.** Cross-carrier comparisons never do.

## Design

### The inbox

`GET /` — replacing the run list — shows every document newest first, in three
buckets:

- **Working on it** — the background task has not finished.
- **Needs you** — one of the four gates stopped it.
- **Done** — it was filed, and the row says what it became.

Bucket membership is computed at read time from what is recorded, with one
exception: "working on it" has nothing to derive from, so `Document` gains a
`status` column. `InboundMessage.processing_status` is the precedent, including
its `CHECK` constraint listing the allowed values.

Read time for everything else matters for the same reason the stuck-documents
spec gave: the state changes the moment she acts, and a written row would be
stale immediately after the act that fixed it.

#### The "needs you" rows

Each carries its fix inline rather than linking away:

| Reason | Shown | Fix on the row |
|---|---|---|
| `needs_client` | first-page snippet, ranked candidates | pick one (`candidates_for` already ranks) |
| `needs_policy` | the client, their policies | pick one, or create from this document |
| `needs_review` | which fields are flagged | opens the detail view |
| `nothing_extracted` | "nothing could be read from this" | detail view: add fields by hand, or retry |

`needs_client` is today's `/unmatched`, moved. The other three are the states
the stuck-documents spec named B, C and D.

#### The "done" rows

A done row says what the document became, not merely that it finished:
*"Renewal term on CAP-7781-22 — compared with the prior term"*, linked to the
comparison. This is how she knows the automatic part worked without visiting
anything. A document classified as something that does not route to extraction
(an invoice, a letter) is done too, and says so: stored and searchable, no term
expected.

#### The receipt

The row appears the instant the file lands, before any processing starts. That
is the confirmation that the document arrived. The page polls while anything is
in flight and stops polling when nothing is.

### Background processing

`POST /documents` (the drop box) and `POST /inbound/mail` both store the blob,
write the `Document` row with `status='processing'`, and return. A background
task then runs `ingest_document`'s existing stages and writes the final status.

No stage changes. They move off the request, which also fixes a latent bug:
inbound mail currently runs OCR and up to three model calls inside the webhook,
and real providers time out around 10-30 seconds and retry — which today means
processing the same message twice.

**The restart case is stated, not hidden.** In-process background work dies with
the process. A document still `processing` after a threshold is shown as
stalled with a Retry button, which re-runs the stages. Every stage is already
idempotent and separately re-runnable — that was step 10's design — so a retry
is safe by construction.

### What is deleted

- `GET /runs/new`, `POST /runs`, `GET /runs/{id}/review`, `POST /runs/{id}/promote`
- `renewal/templates/run_new.html`, `renewal/templates/run_review.html`

`RenewalRun` stays as a table and is never written again. Every comparison built
before this change references it through `renewal_run_id`, and the comparison
screen already renders a run crumb conditionally. This is the precedent
`difference.prior_value` set in step 10: the column stays because it is the only
record of what older rows meant.

`POST /clients` and `POST /policies` survive — creating a policy from a document
is the `needs_policy` fix — and only their redirect target changes.

The three correction endpoints keep their URLs and are rendered by the inbox
detail view.

### The automatic renewal path

After the pipeline promotes a term, if it forms a draft-eligible pair with the
policy's previous bound term — two bound terms of one policy, which is what
`Matrix.draft_eligible` already means — the comparison is built and the draft
generated.

This is exactly what `POST /runs/{id}/promote` does today. It is the same
behaviour reached without a human, not new behaviour: `build_comparison` then
`generate_draft`, both already written, both already exercised.

Two consequences:

- `premium_change` fires from the pipe. The circularity above is closed.
- Nothing is sent anywhere. The draft is text on a screen she copies into her
  own email, exactly as it is today.

**Cross-carrier comparisons are never auto-built.** The rule that nothing
auto-builds earns its keep here and only here: setting competitors side by side
is a recommendation however it is worded, which is why a quoted comparison is
not draft-eligible at all. Step 10 applied that caution to the renewal case as
well, where no recommendation is being made — the columns are one policy's own
history, and the arithmetic is the same either way.

### Notifications

A count beside the inbox link in the nav, always. Plus an email when the
needs-you count crosses from zero to more than zero, sent by the background task
that found it — so no scheduler is required, which matters because
`attention/rules.py:13` records that materialising a time-based reason
"would need a scheduler this design does not have".

Guarded to at most one email an hour by a stored timestamp, and the email lists
everything currently waiting rather than the one document that triggered it. A
bulk import that strands twenty documents sends one email, not twenty.

SMTP settings live in `.env` alongside the existing configuration. This is the
first outbound mail the application sends. The principle it appears to break —
that nothing is sent from here — is about client-facing mail, and remains
intact: no draft, no comparison and no client communication is ever sent
automatically.

### Error handling

- A stage that raises is logged and skipped, as every stage already is. The
  document keeps the furthest status it reached.
- A document stalled by a restart is shown and retryable.
- A retry of an already-finished document is a no-op: every stage checks its own
  work first, and promotion is idempotent per extraction and per blob.
- SMTP failure is logged and never blocks intake. A notification that cannot be
  sent must not cost the document.

## Testing

- Each bucket renders, with the right fix on each `needs you` row.
- A background task flips a document from `processing` to its final status.
- A document stalled mid-flight is listed as stalled and recovers on retry.
- Auto-build fires for a renewal pair; never for a quoted column.
- The deleted routes return 404.
- The notification sends once, not twice, inside the hour, and an SMTP failure
  leaves the document processed.
- **The regression that matters most:** an emailed renewal dec page produces a
  `premium_change` attention item with no human action at all. That is
  impossible today, and it is the single assertion that proves the circularity
  is closed.

## Relationship to the stuck-documents spec

`2026-09-14-stuck-documents-design.md` is superseded, not discarded. Its
analysis of what "stuck" means survives intact and is the basis for three of the
four `needs you` reasons — including the argument for computing state at read
time, and the trap that a document routed but never extracted (the bulk-import
default) is not stuck but not-yet-processed and must not appear.

What changes is the packaging: its `/stuck` queue becomes a filter of the inbox,
and its single-document review screen becomes the inbox detail view. Its
`renewal/stuck.py` module is still the right place for the state computation.

## What this deliberately does not do

- **No re-extraction on a schedule.** Retry is a button a human presses on a
  document she is looking at. A background process that re-runs extraction over
  an archive is how an archive becomes an invoice.
- **No loosening of D8.** A fuzzy client match that files itself would attach
  one client's renewal to another client's policy and report success. The whole
  point of the gates is that the system does not guess about identity.
- **No auto-promotion past a flagged field.** A wrong premium entering the
  record as fact is repeated by every screen downstream without anyone having
  looked.
- **No outbound client mail.** The only mail this adds is to the operator.
- **No worker process, queue, or scheduler.** In-process background work, with
  its one failure mode made visible.
- **No attribution.** Who corrected a field or retried a document is still not
  recorded, deferred system-wide.
