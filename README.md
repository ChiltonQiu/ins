# renewal

Insurance renewal comparison and record layer.

## System dependencies

Beyond the Python dependencies in `pyproject.toml`:

| Dependency | Needed for | Install |
|---|---|---|
| PostgreSQL 15+ | Everything. `pgcrypto` and `pg_trgm` are created by migrations. | `pacman -S postgresql` / `apt install postgresql` |
| Tesseract | OCR of scanned pages during text extraction. | `pacman -S tesseract tesseract-data-eng` / `apt install tesseract-ocr` |

Without Tesseract a scanned document is still stored, hashed, and linked to a
client, but it has no text layer to extract and no OCR fallback — so it is
searchable only by filename, and no dates are pulled from it. The ingest path
raises rather than recording a scanned page as legitimately blank.

OCR runs locally. No scanned page is transmitted to a model provider for text
extraction, whatever `PROVIDER` is set to.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env    # then fill in the values
.venv/bin/alembic upgrade head
```

## Bulk import

Point it at a directory tree and every PDF underneath is stored, text-extracted,
date-extracted, and matched to a client where that can be done safely:

```bash
.venv/bin/python -m scripts.bulk_import /path/to/the/archive
```

Safe to re-run and safe to interrupt. Content addressing makes a second pass
free: a document already known is skipped rather than duplicated. One
unreadable file is counted and logged by path, never by contents, and never
stops the walk.

Import early even if extraction is still weak. Extraction is a pure function of
(blob, extractor_version) and can be re-run at any time; a document deleted from
the source tree before it was imported is gone.

Bulk import does not run structured field extraction. Declarations,
endorsements and quotes are stored, text-extracted, dated, classified and
matched — all of which is local or cheap — but reading the coverage grid out of
them is a model call each, and an archive is thousands of documents. Pass
`--extract-fields` to do it during the import, or leave it and re-run
extraction later against whatever subset is worth it.

Manual upload and email intake do extract, because they are one document at a
time and the result is wanted immediately.

## Blob encryption

Documents are stored unencrypted unless `BLOB_ENCRYPTION_KEY` is set. Generate a
key with:

```bash
.venv/bin/python -c "from renewal.crypto import generate_key; print(generate_key())"
```

Put it in `.env`. New documents are sealed with AES-GCM from then on. To seal a
store that already has documents in it:

```bash
.venv/bin/python -m scripts.encrypt_blobs
```

That is idempotent — already-sealed blobs are left alone — and reads keep
working throughout, so it can run against a live store.

This protects a stolen backup or a copied blob directory. It does not protect a
compromised host: the key sits in `.env` next to the data. Back the key up
separately from the blobs, or they travel together and the encryption buys
nothing. Losing the key means losing every document.

## Accounts

Every page requires a login. Accounts are created from the command line:

```bash
.venv/bin/python -m scripts.add_user anne@agency.com
```

It prompts for the password twice and never takes it as an argument. Running
it again for an address that already exists resets that password, clears any
lockout, and reactivates a deactivated account — that is also how somebody
locked out gets back in. Ten failed attempts lock an account for fifteen
minutes.

Sessions live in the database, so signing out revokes the session rather than
just dropping the cookie. `SESSION_TTL_HOURS` (default 12) sets the length,
and an active session slides rather than expiring mid-task.

Two routes are outside the login, because neither caller can sign in:

- `/calendar/{token}.ics` — the feed carries its own revocable token. Anyone
  holding that URL reads every client name and deadline without logging in.
  Regenerate it from the settings page.
- `/inbound/mail` — the webhook is verified by the inbound provider's
  signature. With `INBOUND_PROVIDER=filedrop` nothing is verified at all, so
  that provider must never be configured on an install reachable from the
  network.

There are no roles: every account can do everything. What each account *did*
is recorded: a correction, a confirmed or dismissed date, a date added by
hand, a cleared attention item, a document filed by hand, a comparison built,
a materiality overridden — each of those rows names the account that made it,
and the calendar and the document detail view show the name.

Rows written before that change, and rows the pipeline wrote rather than a
person, name nobody. They render blank rather than as "system" or "unknown":
those are three different situations and the page does not know which it is
looking at. Configuration — preferences, carrier aliases, the `.ics` token —
is still not attributed.

## Pulling mail from a mailbox

Email is the way documents actually arrive, so the application reads a mailbox
directly rather than waiting to be handed anything.

Point it at an account and a folder, and every few minutes it reads what is
new: the attachments become documents, and so does the message body — the
carrier's explanation is frequently in the body while the attachment is a bare
form, and deadlines are very often stated in prose. Everything then goes
through the same pipeline a manual upload does.

```
IMAP_HOST=imap.gmail.com
IMAP_USER=you@youragency.com
IMAP_PASSWORD=an-app-password
IMAP_FOLDER=Carriers
```

Four things worth knowing before you point it at a real mailbox:

**It only ever reads.** The folder is opened with `EXAMINE` rather than
`SELECT`, so the server itself refuses any write. Nothing is marked read,
moved, flagged or deleted, and a message you have already been sent stays
exactly as your mail client left it. That is structural rather than a promise:
there is no code path that could damage the mailbox even with a bug in it.

**Everything in the folder is ingested**, so point `IMAP_FOLDER` at a folder or
label you filter carrier mail into rather than at `INBOX`. Deciding which
senders count is your mail client's job and it is already good at it; a second,
worse rules engine in here would only disagree with it.

**`IMAP_PASSWORD` must be an app password.** Gmail and Outlook both refuse an
account password over IMAP once two-factor is on. Gmail issues one at
`myaccount.google.com/apppasswords`. There is no OAuth: it would mean a
registered application, a consent screen and a refresh-token store, which buys
nothing for a single agency that can issue itself an app password in a minute.

**Nothing is read twice.** Dedupe is by `Message-ID`, recorded in the database
rather than in the mailbox — which is what lets the mailbox stay untouched. A
UID high-water mark per folder keeps each poll cheap, and it is only an
optimisation: losing it costs a re-read, never a duplicate. If the folder is
rebuilt or renamed, `UIDVALIDITY` changes, the mark is discarded and the folder
is read in full.

One message that cannot be read never stops a poll. It is logged, counted, and
the poll moves past it — each message runs in its own savepoint, because a
failed statement leaves the transaction aborted and would otherwise take every
message behind it in the same run.

When the mailbox stops answering, `/settings` says so: the last attempt, what
it found, and the error if it failed. An intake that has been broken since
Thursday is exactly what the daily summary's "nothing has arrived in N days"
line exists to catch.

The webhook at `/inbound/mail` is unchanged and still there for an agency that
later buys a domain and wants a hosted provider to push instead. Both routes
end in the same intake, so nothing about processing differs between them.

To run the poll from cron instead of in-process, set `IMAP_POLL_SECONDS=0` and:

```bash
*/5 * * * * cd /srv/renewal && .venv/bin/python -m scripts.poll_mail
```

## The daily summary

Everything else here happens because a document arrived. This is the one thing
that happens because a day passed.

Once a day, after `Send that summary after` on `/settings`, one email goes out
saying how many documents need a person, how many items are in the attention
queue, and how many dates fall inside the window — with the nearest one named.
Numbers, a date and a link; no client names, no filenames, nothing that would
put client detail on a mail server. It is the same body the notification sent
when a document finishes processing uses, so the two cannot disagree.

When nothing is waiting, nothing is sent — until `Say so even when nothing
needs me, every` days have passed with no summary at all, and then one goes out
saying exactly that. A quiet week and a forwarding rule that broke on Thursday
are otherwise the same thing from where you sit. A fresh install qualifies
immediately, so the first summary arrives on an empty system: that is how you
learn the mail configuration works.

Two variables set the clock, and both are deployment configuration rather than
preferences:

| Variable | Default | Meaning |
|---|---|---|
| `AGENCY_TZ` | `UTC` | Whose eight o'clock `Send that summary after` means. Everything else in this application is UTC, so leaving this unset sends the summary at four in the morning on the east coast. A name it cannot parse falls back to UTC with a warning rather than sending nothing. |
| `DIGEST_TICK_SECONDS` | `300` | How often the application asks whether the summary is due. `0` turns the in-process clock off. |

The clock is a thread inside the application, so it has the same failure mode
the background intake does: it dies with the process. A day the application is
down for its entirety is a day with no summary — but nothing is lost, because
the summary is computed from what is true rather than from events that might
have been missed. A process that was down at eight and comes up at two sends it
at two, and the next day's summary names the same deadline anyway.

To run it from cron instead, set `DIGEST_TICK_SECONDS=0` and add:

```bash
*/5 * * * * cd /srv/renewal && .venv/bin/python -m scripts.digest
```

The once-a-day record lives in the database, so a cron entry and a running
application cannot between them send two.

## Tests

```bash
.venv/bin/pytest              # excludes tests marked `eval`
.venv/bin/pytest -m eval      # hits the real model provider API
```

Tests need a PostgreSQL database; the default is `renewal_test`, overridable
with `TEST_DATABASE_URL`.
