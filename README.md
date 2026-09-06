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

There are no roles: every account can do everything. Nothing yet records
*which* account made a correction or confirmed a date.

## Tests

```bash
.venv/bin/pytest              # excludes tests marked `eval`
.venv/bin/pytest -m eval      # hits the real model provider API
```

Tests need a PostgreSQL database; the default is `renewal_test`, overridable
with `TEST_DATABASE_URL`.
