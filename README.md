# renewal

Insurance renewal comparison and record layer.

What it does and why it is built the way it is: [docs/how-it-works.md](docs/how-it-works.md). What follows is how to install and operate it.

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
./scripts/install.sh
```

Checks the machine, builds the virtualenv, writes a `.env` from the example,
generates a blob encryption key if there is none, creates the database, runs
the migrations, and offers to make the first account. Safe to re-run: it never
overwrites a `.env`, never regenerates a key that exists, and re-running after
a `git pull` is how you upgrade. It does not install PostgreSQL or Tesseract —
it tells you the command for your distribution and stops.

By hand, if you would rather see each step:

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env    # then fill in the values
.venv/bin/alembic upgrade head
```

`PROVIDER` and the matching API key are required to start. The application
builds its model client at import, so an unset `ANTHROPIC_API_KEY` (with
`PROVIDER=anthropic`) fails at boot rather than at the first document.

## Running it

```bash
.venv/bin/uvicorn renewal.app:app --host 127.0.0.1 --port 8000
```

Then http://127.0.0.1:8000. Every page redirects to `/login` until you have an
account, so make one first — see **Accounts** below.

Two clocks start with the application: the mailbox poll and the daily summary.
Either is turned off by setting its interval to 0 (`IMAP_POLL_SECONDS`,
`DIGEST_TICK_SECONDS`), which is what an install driving them from cron wants.
Intake runs on a thread pool inside this same process, so documents stop being
processed when it stops.

For a first look with no mail and no archive, sign in and drop a PDF on the
inbox page. What the screens do and why they are arranged that way is
[docs/how-it-works.md](docs/how-it-works.md).

## Deploying it

It is one ASGI application, a PostgreSQL database and a directory of blobs.
Two files in `deploy/` cover the usual shape of that:

```bash
sudo useradd --system --home-dir /srv/renewal --shell /usr/sbin/nologin renewal
sudo cp deploy/renewal.service /etc/systemd/system/
sudo systemctl enable --now renewal          # then check: systemctl status renewal

sudo cp deploy/renewal.nginx /etc/nginx/sites-available/renewal
sudo ln -s /etc/nginx/sites-available/renewal /etc/nginx/sites-enabled/
sudo certbot --nginx -d renewal.example.com
```

Both assume `/srv/renewal` and a host of `renewal.example.com`; change those
and, in the unit, `ReadWritePaths` if `BLOB_ROOT` is somewhere else —
`ProtectSystem=strict` makes everything else read-only, and an upload into a
directory that is not listed there fails with a permission error that nothing
on the page explains.

Run it behind a reverse proxy that terminates TLS. `SESSION_COOKIE_SECURE`
defaults to true, so forgetting to configure it fails toward security rather
than away from it; the only reason to set it false is local development over
plain HTTP, where a secure cookie would never be sent and nobody could log in.
Check that value before deploying — the session cookie is the whole of
authentication.

Point the proxy at one uvicorn process. More than one is possible but not free:
each process starts its own poll clock, its own digest clock and its own thread
pool. Nothing breaks — the digest's day row and the mailbox's Message-ID
dedupe both hold across processes — but the work is done more than once. If you
want more than one, set both tick intervals to 0 and drive the two clocks from
cron instead:

```
*/5 * * * * cd /srv/renewal && .venv/bin/python -m scripts.poll_mail
*/15 * * * * cd /srv/renewal && .venv/bin/python -m scripts.digest
```

`INBOUND_PROVIDER=filedrop` verifies nothing and must never be configured on an
install reachable from the network.

## Backing it up

Three things, and they are not interchangeable:

| What | Where | If you lose it |
|---|---|---|
| The database | PostgreSQL | Everything. Documents survive as files nothing can find. |
| The blobs | `BLOB_ROOT` | Every PDF. The database keeps the text and the extracted values, so the record survives; the documents themselves do not. |
| `BLOB_ENCRYPTION_KEY` | The environment | Every blob, permanently. Nothing can decrypt them. |

```bash
pg_dump renewal > renewal-$(date +%F).sql
tar czf blobs-$(date +%F).tar.gz blobs/
```

**Back the key up somewhere the blob backups are not.** A backup that travels
with its key is a backup that is not encrypted. Losing the key is not
recoverable by anyone, including whoever wrote this.

Restoring is `createdb`, `psql < dump`, untar the blobs, and `alembic upgrade
head`. Blobs are content-addressed, so restoring a newer blob directory over an
older database is safe: extra files are ignored, and a missing one is reported
by sha256 when something asks for it.

Losing text or extracted fields is not a restore problem. Text is a pure
function of (blob, extractor version) and extraction is re-runnable from the
blob, so both can be rebuilt with `scripts/reextract.py` as long as the blobs
and the key are intact.

## What it costs to run

Four model calls, on three different models, and they are not the same size:

| Stage | Model | Sent |
|---|---|---|
| Classification | Haiku 4.5 | Page 1's text |
| Dates | Sonnet 5 | The first `DATE_PAGES` pages (default 3) |
| Field extraction | Opus 5 | The whole document's text — or **every page as an image** if the PDF has no text layer |
| Draft | Sonnet 5 | Only on a renewal comparison, and only the two terms |

At current API rates — Opus 5 at $5/$25 per million tokens in/out, Sonnet 5 at
$2/$10, Haiku 4.5 at $1/$5 — a five-page declarations page with a text layer
lands in the neighbourhood of one to three cents all-in, dominated by the Opus
extraction. Classification and dates are rounding errors beside it.

**A scan costs several times more.** With no text layer, extraction rasterizes
every page at 200 dpi and sends images: roughly 2,500 tokens per page against
perhaps 700 for the same page as text. A ten-page scanned policy is the
expensive document in any archive.

Treat those as order-of-magnitude figures rather than a quote — they are
arithmetic over token estimates, not measurements. Every model call records its
model id on the extraction row, so real numbers come from the provider's own
usage reporting once a real archive has gone through.

Two things follow, and both are already wired:

- **`scripts/bulk_import.py` does not extract fields by default.** An archive
  is thousands of documents and this is the stage that costs money. Import
  first, extract later against whatever subset is worth it.
- **Nothing re-extracts on its own.** Extraction is versioned and idempotent;
  re-running it over the corpus is a decision somebody makes, with
  `scripts/reextract.py`, not something a retry does by accident.

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
