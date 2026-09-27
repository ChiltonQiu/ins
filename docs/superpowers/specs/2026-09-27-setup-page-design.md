# The Setup Page — Design

Date: 2026-09-27
Status: draft, for review
Scope: get somebody from "installed" to "it is reading my carrier mail" without
opening `.env`, and without restarting anything.

## Context

After an install, three things stand between the application and being useful,
and every one of them is a hand-edit of `.env`:

- **A model key.** Without one the application does not start at all:
  `renewal/app.py:27` builds the model client at import and `build_client`
  (`renewal/providers.py:159`) raises. There is no page to go to, because there
  is no server.
- **The mailbox.** `IMAP_HOST` is empty by default, and the mail clock is only
  started if it was set when the process began (`renewal/app.py:46`).
- **The summary sender.** `SMTP_*`, likewise.

The installer ends by printing "Put ANTHROPIC_API_KEY in .env". For a person at
an agency this is where the product ends: they will not find `.env`, will not
open it in Notepad, and will not know an app password from their own.

The people this is for are agency staff at offices that have agreed to try the
tool. They will stop at the first thing that does not work. The first real
install was on Gmail, and the design is shaped for Gmail first; any IMAP host
works through the same form.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Where secrets live | `.env`, written by the page | Keeps credentials out of the database, its backups and its exports. The page is a friendlier editor of the same file. |
| How a change takes effect | Live, on save, no restart | A restart is exactly the friction this removes; relaunch also differs by platform. |
| Layout | One `/setup` page, three cards | Serves as the first-run checklist and the place to come back to. No wizard to keep in sync with an edit form. |
| Mail provider | Gmail first; any IMAP host | App passwords work for both IMAP and SMTP on Gmail. Microsoft 365 needs OAuth and is out of scope. |
| Who can use it | Any signed-in user | There are no roles; `/settings` is the same. |

### The rule this changes

`CLAUDE.md` and `renewal/settings_store.py` both say deployment credentials
"never reach a page". That becomes:

> Credentials may be **typed into** `/setup`. They are written only to `.env`,
> never to the database, and never rendered back to any page.

Both files are updated in the same change.

## The page

`/setup`, linked from the nav and from `/settings`. Three cards, top to bottom,
each showing **✓ Working** or **Not set up**.

### Card 1 — AI

- Provider dropdown, defaulting to Anthropic. The other providers in
  `PRESETS` remain available.
- API key field, with a link to where the key is made.
- **Save & test** sends one minimal real request with the proposed key and
  reports the outcome in a sentence.

### Card 2 — Reading mail

Disabled, with a one-line reason, until Card 1 is working. Mail read without a
model is stored but not read, and every one of those documents would need a
Retry afterwards.

- Gmail address, app password, folder.
- Host and port are filled for Gmail (`imap.gmail.com`, 993) and editable
  under "Not Gmail?".
- "How to get an app password" opens Google's app-password page, with three
  lines of explanation beside it.
- **Connect** logs in read-only and lists the folders it finds. The folder is
  then chosen from a dropdown, not typed.
- **Where to start.** Once a folder is chosen the card says how many messages
  it holds and offers:
  - **Only mail that arrives from now on** (default)
  - **Everything already in the folder (N messages)**

  The first poll of a folder otherwise reads all of it, and every document is
  several model calls. On a folder with years of carrier mail that is a bill
  and an afternoon nobody asked for. "From now on" records the folder's
  current highest UID as `mail_poll_state.last_uid` (with its UIDVALIDITY), so
  the existing poller starts from there with no change to its logic.
- **Save & test** logs in, EXAMINEs the chosen folder, and reports
  "Connected. 23 messages in *Carriers*."

### Card 3 — Daily summary email (optional)

- "Send from the same Gmail account", checked by default: no second password.
- "Send the summary to", one address.
- **Save & send a test** sends a real test message and says it went.

Leaving Card 3 empty is a complete setup. Nothing is sent.

### Secrets on the page

A saved password or key renders as `••••• saved (change)`. The input is always
empty. A blank submission means "keep the saved value".

### The banner

Until Cards 1 and 2 are working, every page carries one line:
"Finish setup (1 of 2 done) →". Computed per request from the live settings,
per the repository's "derive state, don't store it" rule: there is no stored
"setup complete" flag to disagree with reality.

## Underneath

### 1. Booting with nothing configured

`build_client` keeps raising for a bad configuration. The composition root
catches the missing-key case and installs an `UnconfiguredClient` whose
`complete()` raises `ModelNotConfigured("AI is not set up yet")`. Every
pipeline stage already logs and skips a failure, so a document uploaded before
setup is stored, text-extracted and searchable, and its model stages are
missing until a Retry.

### 2. `renewal/live.py` — the current settings and client

One object holding the current `Settings` and model client, with `reload()`
that re-reads `.env`, rebuilds both, and swaps them under a lock.

- `Deps.settings` and `Deps.model_client` become properties reading from it.
  The ~20 call sites of `deps.settings` do not change.
- `MailClock` and `DigestClock` take the `Live` object and read it at the top
  of every tick instead of holding the `Settings` they were built with.
- `MailClock` is always started. A tick with no `imap_host` returns
  immediately. This is what makes "turn mail on" work without a restart.
- Work already handed to the runner keeps the settings it was submitted with;
  the next document gets the new ones.

`settings_store.effective()` is unchanged and still layers her preferences over
whatever `Live` currently holds.

### 3. `renewal/envfile.py` — editing `.env`

- Changes only the keys the page owns; every other line, comment and blank is
  left byte-for-byte alone. A missing key is appended.
- Writes to a temporary file in the same directory and `os.replace`s it, so a
  crash cannot leave half a file.
- UTF-8 with **no BOM** — python-dotenv reads a BOM as part of the first key's
  name (`scripts/install.ps1` already learned this).
- Values are double-quoted with `\` and `"` escaped, so a password containing
  `#`, `'`, `"`, `=`, spaces or a trailing backslash round-trips through
  `dotenv_values` unchanged.
- **Keys set outside `.env`.** `load_dotenv` does not override the real
  environment, so a key set by systemd or the shell wins over anything the page
  writes. `envfile` reports which owned keys come from the process environment
  rather than the file; the page shows those fields read-only as "Set by the
  server's environment — change it there", rather than saving a value that
  would silently do nothing.
- `reload()` reads the file with `dotenv_values` and layers it under the real
  environment, matching `load_dotenv`'s precedence, rather than calling
  `load_dotenv(override=True)`.

The keys the page owns: `PROVIDER`, the provider's key variable
(`PROVIDER_KEY_ENV`), `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`,
`IMAP_FOLDER`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`NOTIFY_FROM`. `NOTIFY_TO` stays a preference in `settings_store`, where it
already lives.

### 4. `renewal/setup/checks.py` — the tests behind the buttons

One function per card, each taking **proposed** values (not the live ones) and
returning a `CheckResult(ok, message, detail)`: `message` is the sentence the
page shows; `detail` is the underlying error for the log.

- `check_model(provider, key)` — one minimal completion on the classification
  model.
- `list_folders(host, port, user, password)` and
  `check_mailbox(host, port, user, password, folder)` — via `Mailbox`, so
  EXAMINE (read-only) is inherited, never re-implemented.
- `check_smtp(...)` — sends one test message through `notify.send_email`.

Save runs the check first. On success it writes `.env` and calls `reload()`.
On failure the card shows the message and a small **Save anyway**, so a flaky
network cannot trap anyone out of their own settings.

### 5. Routes

`renewal/web/setup.py`, registered like every other router:

- `GET /setup`
- `POST /setup/model`, `POST /setup/mail/folders`, `POST /setup/mail`,
  `POST /setup/summary`

Behind the existing login gate and the existing same-origin check on POST
(`renewal/web/security.py:48`). The banner is added in
`renewal/web/templating.py`'s nav context, beside the badge.

## Errors

Every check maps the failures somebody will actually hit to one sentence and
one next step. Anything unrecognised shows "Something went wrong:" followed by
the real error — never hidden.

| Card | Failure | Message |
|---|---|---|
| AI | key rejected (401) | That key didn't work. Copy it again from console.anthropic.com → API keys. |
| AI | no credit / billing | The key works, but the account has no credit. Add some under Billing. |
| AI | network | Couldn't reach Anthropic. Is this computer online? |
| Mail | auth failed | Gmail refused the login. It needs an app password, not the normal one. [Make one →] |
| Mail | IMAP disabled | IMAP is turned off for this account. [Turn it on →] |
| Mail | host unreachable | Couldn't reach imap.gmail.com. Is this computer online? |
| Mail | folder empty | Connected. *Carriers* is empty — is the Gmail filter set up? (a warning, not a failure) |
| Summary | auth / send refused | Gmail refused to send. Is it the same app password as above? |

Logged details are passed through a scrubber that removes every value of an
owned secret key before writing.

## Testing

Test-first, against fakes: no real Gmail, no real Anthropic, no cost.

- **envfile** — round-trip of hostile passwords (`#`, both quotes, `=`,
  spaces, `\` and a trailing `\`, non-ASCII); untouched lines byte-identical;
  missing keys appended; no BOM; atomic write leaves the old file on failure;
  keys from the process environment reported and never written.
- **live** — after `reload()` the next request and the next clock tick see the
  new values; a tick with no `imap_host` does nothing and does not raise; the
  application boots with no key and a model stage fails as
  `ModelNotConfigured` with the document kept.
- **checks** — every row of the Errors table, against a fake IMAP server, a
  fake SMTP server and a fake model client.
- **routes** — saves on a passing check; "Save anyway" saves on a failing one;
  a blank secret keeps the stored value; a secret never appears in any
  response body; Card 2 refuses while Card 1 is not working; "from now on"
  sets `last_uid` to the folder's current maximum; the banner counts 0, 1 and 2
  and disappears at 2.

## Not in this design

- **Microsoft 365 / OAuth.** Basic-auth IMAP is off on Exchange Online. That
  is its own design.
- **Model names, worker counts, ports, database.** Deployment tuning, set once
  by the installer; they stay in `.env`.
- **Tesseract.** The installer finds it and writes `TESSERACT_CMD`.
- **Roles.** Anyone signed in can change setup, as with `/settings` today.

## Release context

This is one of four pieces going into 0.3.0, each with its own spec:

1. **Windows install fixes** — done (`fix/windows-first-run`), shipping first
   as 0.2.6.
2. **Windows CI** — a GitHub Actions job running the full install on a clean
   Windows runner on every push, with the preinstalled Python and PostgreSQL
   hidden so the download path is exercised.
3. **This page.**
4. **One-click update** — designed after this, since its version line and
   Update button live on this page.

UI polish and onboarding beyond this page wait until the first user has been
watched using it.
