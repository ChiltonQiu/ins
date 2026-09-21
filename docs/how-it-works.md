# What this does

A record layer and a renewal comparison tool for one insurance agency.

Documents arrive — usually by email, sometimes by upload, once in bulk from an
archive. Each one is stored, read, dated, labelled, and matched to a client.
Some become structured policy terms. When a renewal lands on a policy that
already has a term, the comparison between them builds itself and says what
changed and whether it matters.

Two things are true of every part of this and are worth stating before the
detail, because most of the design follows from them:

**No model output is allowed to gate anything.** A document nobody could
classify and nobody could match to a client is still stored, still searchable
by its text, and its dates still appear on the calendar. Extraction accuracy
determines how much work is saved, never whether a document can be found.

**State is computed, not stored.** What a document needs is worked out at read
time from rows the pipeline already wrote. The one exception is
`document.status`, which records that work is in flight, because that has
nothing to derive from.

---

## The way in

Three doors, one pipeline. Everything ends in `ingest_document`
(`renewal/pipeline.py`).

| Door | How | Field extraction |
|---|---|---|
| Bulk import | `python -m scripts.bulk_import <dir>` | Off by default (`--extract-fields` to turn on) |
| Manual upload | `POST /documents` from the inbox page | On |
| Email | IMAP poll, or the `/inbound/mail` webhook | On |

Bulk import differs only in that it does not run structured field extraction:
an archive is thousands of documents and each extraction is a model call.
Everything else — storage, text, dates, classification, client matching — runs
the same way for all three.

### Mail

The poller (`renewal/mail/poll.py`) logs into one mailbox, opens one folder
**read-only**, and reads what is new. Read-only is structural rather than
promised: the folder is opened with `EXAMINE`, so the server itself refuses any
write this code could issue. Nothing is marked, moved, flagged or deleted, and
a message stays exactly as the mail client left it.

Both the attachments and the message body become documents. Carriers routinely
put the explanation in prose and attach a bare form, and deadlines are very
often stated in the body.

Nothing is read twice. Dedupe is by `Message-ID`, recorded in the database
rather than in the mailbox — which is what lets the mailbox stay untouched. A
UID high-water mark per folder keeps each poll cheap and is only an
optimisation: losing it costs a re-read, never a duplicate. If the folder is
rebuilt or renamed, `UIDVALIDITY` changes, the mark is discarded, and the
folder is read in full.

One message that cannot be parsed never stops a poll. Each message runs in its
own savepoint, is logged and counted, and the poll moves past it.

The webhook at `/inbound/mail` is still there for an installation that has a
domain and a hosted provider. It routes by recipient address — never by sender,
which is forgeable and which forwarded mail gets wrong. Mail matching no agency
is stored `quarantined` and produces no documents, because dropping it silently
would make a misconfigured forwarding rule invisible. The poller names its
agency instead, since logging into the account settles the question.

---

## What happens to a document

In order (`run_stages`, `renewal/pipeline.py:299`). Every stage after storage is
best-effort and separately re-runnable: a stage that raises is logged and
skipped, because losing the document because OCR failed would be worse than a
document with no text yet. Every stage is idempotent, which is what makes the
Retry button safe.

**1. Stored.** The blob's filename is its own sha256
(`renewal/blobstore.py`), so the store is immutable by construction and an
identical document delivered twice deduplicates for free. With
`BLOB_ENCRYPTION_KEY` set, blobs are sealed at rest. The row is written inside
the request; the stages below run after the response has gone out, on a thread
pool (`renewal/background.py`).

**2. Text.** The embedded text layer, with Tesseract OCR as the fallback for a
scan. OCR runs on this machine — no page is sent to a model provider for text
extraction, whatever `PROVIDER` says. Text is versioned by extractor, so a
better extractor can be re-run over the whole corpus.

**3. Client matching** (`renewal/resolve/service.py`). Auto-linking requires
exactly one candidate at the threshold, which in practice only an exact
policy-number match reaches. No match, a name-only match, or two equally exact
matches all leave the document unlinked and in the queue. A document with no
link row is unmatched — there is no status column that could disagree with
reality. A manual assignment writes a new link carrying the ranked list that
was on screen: these were offered, this was right.

**4. Dates** (`renewal/dates/service.py`). Two passes, a regex pass and an LLM
pass, both stored, so their recall can be measured separately. A date whose
cited source text is not on the page it names is **rejected**, not stored at low
confidence — a date renders on a calendar as a claim, and a confidently wrong
cancellation date is the worst thing this system can do. With no model
configured the regex pass still runs, so there is a recall floor rather than no
dates.

**5. Classification** (`renewal/classify/runner.py`). One coarse label from
eleven: declarations, endorsement, cancellation notice, non-renewal notice,
invoice, id card, loss run, inspection report, quote, correspondence, unknown.
It runs *after* dates on purpose, so date extraction cannot depend on a label —
made impossible rather than merely untrue today. The label routes: only
`declarations`, `endorsement` and `quote` go on to field extraction.

**6. Field extraction** (`renewal/extract/`). A model reads the coverage grid
into typed field paths — `policy.total_premium`,
`coverage.<name>.limit_value`, `item.<id>.coverage.<name>.deductible_value`,
`forms.<id>.edition_date` and so on, under one grammar shared by extraction,
corrections, the diff and the materiality rules (`renewal/fieldpath.py`), so a
rule, a correction and an eval fixture can all name the same thing the same
way. Every field carries a value, a confidence, a source page and the source
text it claims to be quoting. Extraction never mutates an earlier one: a retry,
a prompt change or a new model all write new rows.

**7. The verification gate** (`renewal/extract/validate.py`). Every field's
cited source text is checked against the page it cites. A field that fails is
**kept at zero confidence**, not discarded — an unverifiable field is evidence
about the extractor, and the corpus is the point. Zero confidence sends it to
review and keeps it out of the draft.

**8. Promotion** (`renewal/promote.py`). A clean extraction, plus any
corrections, is frozen into a `policy_term`: written once, never updated. A
later correction or a better extractor produces a *new* term, so every
comparison keeps pointing at exactly the values it was computed from. An
extraction that produced nothing at all is not a clean extraction and does not
promote — that check exists because an empty term would silently become what
the client page shows for a policy that has a perfectly good prior one.

**9. Comparison, unasked.** If the promoted term is a renewal of an existing
one, the comparison builds itself and a draft is written. This is what puts a
premium change in front of the operator instead of behind a click somebody has
to know to make.

**10. Attention.** Rules read the label, the link and the term the stages above
wrote, and flag what looks like it needs a human.

---

## The screens

### The inbox (`/`)

Every document in one of four buckets, computed on every read:

- **working** — in flight right now
- **stalled** — in flight for too long; a restart can strand a document, and
  this is where that shows, with a Retry button, rather than being pretended
  away
- **needs you** — five reasons: `failed`, `needs_client`, `needs_policy`,
  `needs_review`, `nothing_extracted`
- **done**

The checks run from "we know least" to "we know most", so the reason on the row
is the earliest thing that stopped the document rather than the last thing that
noticed. The common fixes — assign a client, create a client, name a policy —
are inline on the row. The detail view shows one document's fields, its
corrections and its retry.

### Corrections

Correcting a field does not edit it. It writes a row saying what the model
produced, what the truth was, and which extractor version was responsible
(`renewal/corrections.py`). Three kinds: `wrong_value`, `omission`,
`hallucination`. This is the asset: it turns real use into a labelled
evaluation set, exportable with `scripts/export_corrections.py`.

### Comparison (`/policies/{id}/compare`, `/comparisons/{id}`)

One baseline term and up to four comparands. A two-term renewal diff is just
the two-column case.

Every difference is emitted — nothing is suppressed at diff time, so the audit
trail stays complete and it stays visible how often each noise rule fires
(`renewal/diff.py`). Values compare only when their field paths describe the
same thing on the same vehicle.

Each difference is then labelled **material**, **informational** or **noise** by
ordered rules in `config/materiality.yaml`, first match wins, and the rule that
matched is recorded on the row. A naive diff produces around forty differences
per renewal of which perhaps three matter; the filtering is the product, which
is why it lives in a tunable config file rather than buried in a prompt.
Reclassifying in the UI logs a reclassification rather than editing the
difference — that log is the evidence for which rules are wrong.

Premium attribution (`renewal/premium.py`) is arithmetic only: every line is a
subtraction of two numbers printed on the documents. Whatever the line items do
not account for is reported as an unattributable residual, because claiming a
rate increase the page does not state is exactly the error nobody would catch.

The draft (`renewal/draft.py`) explains what changed. It does not advise —
advising is licensed activity — and it is written for a licensed human to read
and edit.

### Client page and prep sheet (`/clients/{id}`, `/clients/{id}/prep`)

Everything about one client assembled for reading while on the phone: dense,
complete, nothing basic hidden behind a click. Every value is either a stored
fact or an explicit "unknown". Nothing infers.

### Calendar (`/calendar`) and the `.ics` feed

Every date that needs to be seen, in one list. An entry escalates when the
document was classified as a cancellation or non-renewal notice **or** when the
date's own type says so — doubled up on purpose, so a misclassified notice
still escalates and display never depends on classification being right.

Dates carry no client id; the client comes from the document's latest link, so
re-filing a misfiled document moves every date on it at once.

The `.ics` feed is subscribe-only, through a revocable token. Nothing is ever
written into anyone's calendar and there is no two-way sync. An unconfirmed
date is labelled as such in its summary, because it is leaving for an app that
knows nothing about confirmation state.

### Attention (`/attention`)

Six reasons: cancellation notice, non-renewal notice, renewal received,
premium change, unmatched document, unconfirmed date coming up soon. Five are
written once at ingest; the last is computed at read time because it changes
with the clock.

Deliberately not a task manager. Every item is cleared by hand and nothing
auto-resolves — detecting that someone replied would mean reading sent mail.
The rules bias toward over-flagging: a wrong flag costs one keystroke, a missed
cancellation notice is the risk this whole system exists to reduce.

### Search (`/search`)

Four ways in — page text, client name, policy number, carrier name — unioned to
one row per document. A document is findable whether or not it was classified
and whether or not it was matched, because those involve judgment and this must
not. Ordered newest-first rather than by relevance: someone on the phone is
almost always after the most recent thing about a client, not the best textual
match.

### Settings (`/settings`)

A whitelist, and the split is deliberate. What lives here is judgment about how
the agency wants to work — whether to notify, who to notify, the minimum
interval, whether to send the daily summary and at what hour, quiet days, the
premium-change threshold, the unconfirmed-date window, session length. Changed
from the page, effective on the next read, no restart.

What is *not* here is deployment configuration — model names, SMTP and IMAP
hosts and credentials, worker counts, poll intervals. Those are set against the
machine, and putting them on a screen would only invite someone to change them
without a reason to. The page also shows the mailbox's last poll, what it
found, and the error if it failed.

---

## What it sends

Two emails, both to the operator, never to a client.

**The needs-you notification** (`renewal/notify.py`) — event-triggered, by the
background task that produced the backlog, behind a minimum-interval guard, an hour by default.

**The daily summary** (`renewal/digest/`) — clock-triggered, once a day after a
configured hour, skipping quiet days. It says how many documents need a person,
how many items are in the attention queue, how many dates fall in the window,
and names the nearest one. Numbers, a date and a link: no client names, no
filenames, nothing that would put client detail on a mail server. It is also
what catches an intake that has been silently broken since Thursday.

Both send the same body — one question asked by two clocks, not two
notification systems. The digest's send is recorded *after* the send and never
before, so the worst a crash can do is send twice rather than silently skip a
day.

**No draft, no comparison and no client communication is ever sent
automatically.** There is currently no path at all — manual or automatic — for
getting a comparison or a draft out of the browser.

---

## Who did it

Every decision table carries a `user_id`: corrections, date confirmations and
dismissals, clearing and filing, comparisons. And it shows on the page — an
attributed decision nobody can see is not attributed.

Login is a session cookie, and the gate covers every route; an unauthenticated
request to anything is a redirect to `/login`. Accounts are created with
`scripts/add_user.py`. Session length is a setting.

---

## The rules this obeys throughout

1. Model output never gates search or the calendar.
2. State is computed at read time; a stored status would lie the moment
   somebody fixed the thing.
3. Anything a comparison points at is frozen and never updated in place.
4. Every stage is idempotent and separately re-runnable, so retry is always
   safe and a failed stage never costs the document.
5. Nothing is suppressed where suppressing it would hide how often a rule
   fires.
6. A wrong flag beats a missed one.
7. Correcting something writes a row rather than editing one, because the
   record of being wrong is the asset.
8. The mailbox is opened so that the server refuses writes, rather than trusted
   not to issue them.

---

## What it does not do

- **Advise.** Drafts explain; they never recommend.
- **Send anything to a client**, or offer any way to export a comparison.
- **Serve more than one agency.** The agency id is hardcoded to 1 in three
  places.
- **Match carriers fuzzily.** Exact match on a normalized name or alias only —
  picking the wrong company would be invisible, and admitted status changes how
  a policy should be read. Admitted status is per state, and an unknown is left
  unknown.
- **Auto-resolve anything in the attention queue.**
- **Write to a mailbox or a calendar.** Both are read-only, structurally.
- **Run OAuth**, for mail or anything else. An app password is what it expects.
- **Use a queue or a worker process.** A thread pool in the application is the
  whole mechanism, which is the right size for an agency of a few people and
  adds nothing to deploy.

---

## Its current state, honestly

893 tests pass, 37 tables, every planned feature built and merged. What has not
happened is contact with reality: `evals/baselines/` is empty, no real mailbox
has ever been polled, and the development database holds four clients against
24 KB of synthetic documents. Extraction accuracy on genuine carrier paperwork
is, as of this writing, unmeasured.
