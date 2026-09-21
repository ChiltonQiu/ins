# Working in this repository

Read `docs/how-it-works.md` before changing anything. It explains what the
system does and, more importantly, why each part is shaped the way it is.

## The conventions that are easy to break by accident

**No model output may gate retrieval.** Search and the calendar must work for a
document that was never classified and never matched to a client. A change that
makes findability depend on an extraction is a bug however well it tests.

**Derive state, don't store it.** What a document needs is computed at read time
from rows the pipeline already wrote (`renewal/inbox.py`). The single stored
status is `document.status`, because work in flight has nothing to derive from.
Adding a status column is almost always the wrong fix.

**Anything a comparison points at is frozen.** `policy_term` is written once and
never updated. A correction or a better extractor writes a *new* term, so an old
comparison keeps showing exactly the values it was computed from.

**Corrections and reclassifications append, never edit.** A correction says what
the model produced, what the truth was, and which extractor version was
responsible. That record is the evaluation set; editing the field in place
destroys it.

**Every pipeline stage is idempotent and best-effort.** A stage that raises is
logged and skipped — the document survives with that stage missing. This is what
makes Retry safe and what keeps a failed OCR from costing a document.

**Extraction is versioned and re-runnable.** Never mutate an extraction. New
prompt, new model, new run: new rows, so any version can be re-run over the
corpus and compared.

**Prefer over-flagging to missing something.** A wrong attention item costs one
keystroke. A missed cancellation notice is the risk the system exists to reduce.

**The mailbox is read-only structurally.** `EXAMINE`, never `SELECT`, so the
server refuses writes rather than this code being trusted not to issue them.
Nothing may mark, move, flag or delete a message.

**Nothing client-facing is ever sent.** The two operator emails carry counts, a
date and a link — never client names or filenames. Drafts explain what changed;
they never advise, because advising is licensed activity.

## Layout

One pipeline (`renewal/pipeline.py`) behind three doors: bulk import, upload,
mail. `renewal/web/` is routers, `renewal/templates/` is Jinja, and the domain
logic lives in the packages beside them (`extract/`, `dates/`, `classify/`,
`resolve/`, `attention/`, `search/`, `calendarview/`, `digest/`, `mail/`).

Field paths follow one grammar (`renewal/fieldpath.py`) shared by extraction,
corrections, the diff and the materiality rules. Materiality lives in
`config/materiality.yaml` so it can be tuned without touching a prompt.

Operator preferences live behind a whitelist in `renewal/settings_store.py` and
are changed from `/settings`. Deployment configuration — model names, hosts,
credentials, intervals — lives in the environment and never on a page.

## Practicalities

- Tests: `.venv/bin/pytest` (needs PostgreSQL; `TEST_DATABASE_URL` overrides).
  `-m eval` hits the real provider API and costs money.
- Migrations: Alembic. Write the downgrade, and check it runs.
- The app builds its model client at import, so it will not boot without
  `PROVIDER` and the matching API key.
- Plans and specs live in `docs/superpowers/`. A superseded spec says so in its
  own header rather than being deleted.

## Known debt

- `renewal_run` is a dead table: never written, kept for old rows.
- Agency id is hardcoded to 1 in `settings_store.py`, `mail/poll.py` and
  `web/settings.py`. There is no tenancy.
- `evals/baselines/` is empty. Extraction accuracy on real documents has never
  been measured, which makes it the biggest open question in the project.
