# Polling a Mailbox — Design

Date: 2026-09-16
Status: approved for planning
Scope: pull mail and its attachments out of a real mailbox on a schedule, so
that intake happens without anybody dropping a file into a form.

## Context

`renewal/mail/` has a complete inbound path — parse the MIME, dedupe by
Message-ID, quarantine an unknown recipient, store the body as a document
beside the attachments, hand each attachment to the pipeline. `receive()` in
`renewal/mail/intake.py` does all of it and is already transport-agnostic: it
takes an `InboundEmail` and does not care where it came from.

What is missing is anything that produces an `InboundEmail` in production.

The one implementation of `InboundProvider` is `filedrop`, which reads `.eml`
files from a directory and verifies nothing. Its `drain()` is called from a
test and nowhere else, so even that does not auto-import. `provider.py:3` says
the hosted choice — Postmark, CloudMailin, SES — was deliberately deferred.

### Why a webhook cannot be the answer here

All three hosted options are *push*: they POST to a URL. That needs a domain,
an MX record, a provider account and a publicly reachable address. This
application runs on one machine behind a home connection. The webhook route is
worth keeping — it is the right shape for an agency that later buys a domain —
but it cannot be exercised on the install that exists today, and an intake path
that cannot run is not intake.

Pulling is the opposite: the mailbox already exists, already receives the
carrier mail, and IMAP needs nothing but credentials. Python has `imaplib` in
the standard library, so this costs no dependency.

## Decisions

1. **Both transports, one intake.** The poller produces `InboundEmail` and
   calls the same `receive()`. Nothing about parsing, dedupe, quarantine, body
   handling or the pipeline is duplicated or forked, and the webhook keeps
   working unchanged for the day a hosted provider is chosen.
2. **For pulled mail, the credentials are the routing.** `receive()` routes by
   recipient (`intake.py:5`) because a webhook is a public door and the
   envelope is the only evidence of who was meant to get it. A polled mailbox
   is a different situation: logging into *this account* and *this folder*
   already answers whose mail it is. It also has to work this way — the
   intended workflow is that carrier mail is forwarded or filtered into a
   folder, and a forwarded message keeps the original `To:`, so recipient
   matching would quarantine every single message. `receive()` gains an
   optional `agency` argument; the webhook passes nothing and behaves exactly
   as before.
3. **The mailbox is opened read-only.** `EXAMINE`, never `SELECT`. Nothing is
   marked, moved, flagged or deleted — the server itself refuses the write, so
   this is structural rather than a promise in a comment. A tool that reads a
   person's real mail should not be able to damage it even if it has a bug.
4. **The folder is the queue; Message-ID is the ledger.** Because nothing is
   marked read, "what have I already seen" cannot live in the mailbox. It lives
   where it already lived: `InboundMessage.message_id`, unique per agency. A
   message pulled twice returns the existing row and costs nothing.
5. **UIDs make the poll cheap; dedupe keeps it correct.** The high-water UID
   per folder is remembered so each poll fetches only what arrived since. If
   `UIDVALIDITY` changes — the folder was rebuilt or renamed — the mark is
   discarded and the folder is read in full, because a UID from the old
   numbering means nothing. Correctness never rests on the UID: it rests on
   decision 4.
6. **One bad message does not stop a poll.** A message that cannot be parsed is
   stored as a blob with an `InboundMessage` row at `processing_status='failed'`
   and the poll continues past it. Evidence rather than a silent skip, and no
   retry loop that jams the mailbox behind one malformed message.
7. **Credentials live in the environment.** Host, user, password and folder are
   deployment configuration, exactly as `SMTP_*` is. `settings_store.py:9` is
   the rule: what goes on `/settings` is a judgment about how the agency works,
   and a password is not one.
8. **The clock is the one this application already has.** A daemon thread
   ticking on an interval, started from `renewal/app.py`, with the same stated
   failure mode as the digest clock and the intake runner: work in this process
   dies with this process. A tick that raises is logged and the loop continues.

## Design

### The connection

`renewal/mail/imapbox.py`, one class over `imaplib.IMAP4_SSL`:

```python
class Mailbox:
    def __init__(self, host, user, password, *, folder, port=993): ...
    def __enter__(self) -> Mailbox   # connects, logs in, EXAMINEs the folder
    def uid_validity(self) -> int
    def uids_since(self, uid: int | None) -> list[int]
    def fetch(self, uid: int) -> bytes        # raw RFC822
```

It returns raw bytes and knows nothing about documents. `parse_mime` already
turns those bytes into an `InboundEmail`, and already falls back to a content
hash when a message carries no `Message-ID` (`parse.py:52`), so dedupe survives
malformed mail.

### The poll

`renewal/mail/poll.py`:

```python
def poll_once(session, store, *, settings, client, on_document=None) -> PollResult
```

1. Read the remembered `(uid_validity, last_uid)` for this host/folder.
2. Connect. If `UIDVALIDITY` differs from what was remembered, forget the mark.
3. `uids_since(last_uid)` → fetch each in order.
4. Parse and `receive(...)` each, with the agency passed in (decision 2).
5. Advance the mark to the highest UID *fetched*, whatever happened to it, so a
   message that failed to parse is never re-fetched forever.
6. Return counts: seen, ingested, duplicate, failed.

The state is one row per `(host, folder)` in a new `mail_poll_state` table.
Not `agency_setting`: that table is the operator's preferences with a
whitelist, and a UID high-water mark is neither a preference nor hers.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `IMAP_HOST` | *(empty)* | Empty turns polling off entirely, the same way an empty `SMTP_HOST` turns notifications off. |
| `IMAP_PORT` | `993` | |
| `IMAP_USER` | *(empty)* | |
| `IMAP_PASSWORD` | *(empty)* | An app password. Gmail and Outlook both refuse an account password over IMAP once 2FA is on. |
| `IMAP_FOLDER` | `INBOX` | Set this to the folder or label carrier mail is filtered into. |
| `IMAP_POLL_SECONDS` | `300` | `0` turns the in-process poller off, for an install driving `python -m scripts.poll_mail` from cron. |

### What the operator sees

Polling is invisible when it works, which is correct — a document simply
appears in the inbox. When it does not work it must not be invisible, so the
settings page grows one line: the last successful poll, what it found, and the
last error if the most recent attempt failed. An intake that has been broken
since Thursday is exactly the failure the daily summary exists to surface, and
`digest.content` already reports how long it has been since anything arrived.

## What this does not do

- **It never writes to the mailbox.** No delete, no move, no flag, no marking
  read. Decision 3 makes that structural.
- **It does not send mail.** This connects to IMAP only. Outbound is still
  SMTP, still operator-only, still one email.
- **No OAuth.** App passwords only. OAuth for Gmail means a registered
  application, a consent screen and a refresh-token store — a project of its
  own, and one that buys nothing for a single agency that can issue itself an
  app password in a minute.
- **It does not filter.** Everything in the configured folder is ingested. The
  filtering is the mail client's job, and it is already good at it; re-deciding
  which senders count would put a second, worse rules engine in this codebase.
- **It does not replace the webhook.** Decision 1.
- **It does not read more than one folder.** One mailbox, one folder, one
  agency, matching the tenancy this application actually has.
