# Attribution — Design

Date: 2026-09-15
Status: approved for planning
Scope: record which account made each human judgment about the record, and
show it where the judgment shows.

## Context

Four specs in a row have deferred this, each noting it as a known absence:

- `2026-09-05-authentication-design.md:6` — "recorded decisions to a user is
  explicitly deferred", and `:142` — `is_active` exists "so an account can be
  turned off without deleting rows that a future attribution change will want
  to point at."
- `2026-09-12-comparison-matrix-design.md:899` — "Nothing records who built a
  comparison, who reclassified a row, or who printed a prep sheet — consistent
  with the authentication spec, and **still wrong the day a second person uses
  this**."
- `2026-09-14-stuck-documents-design.md:248` and
  `2026-09-14-automatic-intake-design.md:250` — "deferred system-wide".

`README.md:110` says it plainly: "Nothing yet records *which* account made a
correction or confirmed a date."

The shape was left ready on purpose. `DateEvent`, `ManualDateEvent` and
`AttentionEvent` already carry an `actor` column, and every row in the database
says the same thing in it: the literal string `human`. That column answers
"was this a person or the machine". It was never able to answer "which
person", and with one account it never had to.

### Why it stops being cosmetic with a second account

Every one of these tables exists because a human overrode the system. A
correction is training data — `renewal/models.py:197` calls the corrections
table "the long-term asset". A dismissed cancellation date is a decision that
something is not an emergency. An attention item cleared is a claim that it was
handled.

With one account those rows are anonymous and it costs nothing. With two, every
one of them becomes a question the database cannot answer: who decided this,
and should I ask them before I undo it. The corrections table in particular
stops being usable as training data the moment nobody can tell a senior
producer's correction from a temp's typo.

## Decisions

1. **`user_id` beside `actor`, never instead of it.** `actor` keeps saying
   human-or-machine; `user_id` says which human. A row written by the pipeline
   has `actor='auto'` and no user, and that is not a gap — there is genuinely
   no person to name.
2. **Nullable, permanently.** Every row written before this change has no user
   and none can be invented for it. A migration that guessed — attributing the
   whole history to the only account that exists today — would manufacture
   evidence, which is worse than an honest blank.
3. **The gate supplies it.** `renewal/web/security.py` already resolves the
   signed-in user on every request and puts their email and display name on
   `request.state`. It gains `user_id`, which is the one field attribution
   needs and the only reason to add it.
4. **The library never learns about requests.** Every service function takes
   `user_id: int | None = None`, exactly as it already takes `actor`. Nothing
   below `renewal/web/` imports anything about a request, and a test can still
   call `confirm(session, id)` without inventing a user.
5. **A user who decided something cannot be deleted.** The foreign keys are
   `ON DELETE RESTRICT`, not `CASCADE`. `UserSession` cascades because a
   session is worthless without its user; a correction is not. `is_active`
   already exists to turn an account off, and that is the supported move.
6. **It records judgments about the record, not configuration.** Who corrected
   a field, confirmed a date, cleared an item, filed a document, built a
   comparison, reclassified a difference. Not who changed a setting or added a
   carrier alias — see "What this does not do".
7. **Where the decision shows, the person shows.** A stored attribution nobody
   can read is a migration, not a feature.

## Design

### The columns

Seven tables gain `user_id INTEGER NULL REFERENCES app_user(id) ON DELETE
RESTRICT`:

| Table | The question it answers |
|---|---|
| `correction` | Who corrected this field. The training-data one. |
| `date_event` | Who confirmed or dismissed this extracted date. |
| `manual_date` | Who added this date by hand. |
| `manual_date_event` | Who dismissed it. |
| `attention_event` | Who cleared this item. |
| `document_link` | Who filed this document, when `method='manual'`. |
| `comparison` | Who built this comparison. |
| `reclassification` | Who overrode this materiality. |

That is eight rows in the table and seven tables named in the paragraph above
it, because `manual_date` and `manual_date_event` are one question asked twice.

`correction` and `reclassification` have no `actor` column and gain no
`user_id` default: both are written only by a human acting, so a NULL there
after this change means a row from before it.

### The plumbing

`renewal/web/deps.py` gains one function:

```python
def acting_user_id(request) -> int | None:
    return getattr(request.state, "user_id", None)
```

`getattr` with a default rather than a bare attribute read, because the two
public routes — the `.ics` feed and the mail webhook — never pass the gate and
have no user on their state. Neither writes an attributed row today, and this
is what makes it safe if one ever does.

Routes that do not currently take a `Request` will take one. That is the whole
change at most call sites: one parameter in, one keyword out.

### Where it shows

- **The document detail view** does not list corrections at all today: a
  corrected field shows its effective value and nothing about the correction
  that produced it. It gains a short list of them — field, what it became, who
  — which is the first place in the application the corrections table is
  visible to the person filling it.
- **The calendar** shows a confirmed or dismissed date. The row gains who did
  it, on the same line as when.
- **The attention queue** is a list of what is *not* cleared, so nothing there
  changes. The person who cleared an item is recorded and readable from the
  row's events; putting it on a screen belongs with a history view that does
  not exist yet.

Display uses `display_name`, never the email address. The address is a login
credential and a way to contact someone, and neither belongs in a rendered
list of who touched what.

### What a row with no user means

Three distinct things, and the interface must not claim to tell them apart:

1. Written before this change.
2. Written by the machine (`actor` says so).
3. Written by the mail webhook or another unauthenticated path.

The templates render nothing at all when `user_id` is NULL — no "unknown", no
"system". A blank is honest; a label is a guess.

## What this does not do

- **It does not attribute configuration.** Who changed a preference, added a
  carrier alias, or regenerated the `.ics` token is still not recorded.
  `agency_setting` is already append-only and would take the same column
  cleanly; it is left out to keep this change to judgments about a client's
  file, and noted here so the absence is known rather than surprising.
- **It does not add roles.** Every account can still do everything.
  Recording who did a thing and restricting who may do it are separate
  changes, and only the first one is honest about what the data says.
- **It does not backfill.** See decision 2.
- **It does not record who read anything.** No access log, no view tracking.
  This records decisions, which are writes.
- **It does not record who printed a prep sheet or retried a document.**
  Neither writes a row at all today; attributing them means first deciding
  they are worth recording, which is a different question from this one.
