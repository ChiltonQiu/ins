# Authentication — Design

Date: 2026-09-05
Status: approved for planning
Scope: gate the web application behind named user accounts. Attribution of
recorded decisions to a user is explicitly deferred.

## Context

The record layer holds the whole book of business: client names, policy terms,
premiums, the text of every document ever imported, and the deadline calendar.
The application in front of it has no authentication of any kind. Every route
is reachable by anyone who can reach the port.

Phase 2 listed "auth, accounts, multi-tenancy, billing" as non-goals. This
spec takes the first of those and only the first: a login, named accounts, and
a default-deny gate. Multi-tenancy is still a non-goal — there is one agency
row and this design does not change that.

Two routes already carry their own credential and must keep working without a
cookie:

- `GET /calendar/{token}.ics` — a bearer token in the URL, because the URL is
  pasted into a phone's calendar configuration. A calendar client cannot log
  in and will never present a session cookie.
- `POST /inbound/mail` — verified by the inbound provider's own signature
  check. The caller is a mail provider, not a person.

## Decisions

Settled during brainstorming, recorded here so the plan does not relitigate
them:

| Question | Decision |
|---|---|
| Identity | Named per-person accounts, not one shared login |
| Provisioning | A CLI script only. No signup route, no invite mail, no reset flow |
| Session transport | Opaque token in a cookie, backed by a `user_session` table |
| Attribution | Out of scope. This change gates access and records nothing new about who acted |

## Non-goals

Not built here. Ask before adding any of them.

Roles or permissions of any kind — every account can do everything. Password
reset by email. Email verification. Self-service signup. Multi-tenancy or any
per-user scoping of data. Remember-me beyond the session TTL. Two-factor
auth. An in-app user administration screen. OAuth or SSO. Actor columns on
`Correction`, `DateEvent`, `AttentionEvent`, `Reclassification`,
`ManualDateEvent`, or promotion.

## Architecture

### Enforcement: default-deny middleware

A single Starlette middleware registered in `create_app`. Every request
requires a valid session except an explicit public allowlist.

The alternative — a `Depends(require_user)` on each `APIRouter` — was
rejected. There are eleven `register()` sites plus the index route defined
inline in `renewal/web/__init__.py`, and an omission at any one of them is a
silent hole that no test naturally catches. Default-deny means a route added
next month is protected the day it is written, and the failure mode of
forgetting the allowlist is a locked door rather than an open one.

This matches how the rest of the codebase reasons: `build_inbound_provider`
refuses to guess a provider rather than falling back to the one that trusts
everybody, and `_one_of` rejects an unexpected value rather than coercing it.

The public allowlist is exactly five entries:

```
/login              GET and POST
/logout             POST; see below
/static/*           stylesheet and script
/calendar/{t}.ics   carries its own token; a calendar client cannot log in
/inbound/mail       carries the provider's signature; the caller is a machine
```

`/logout` is public so that clicking it with an already-dead session clears
the cookie and lands on the login page. Behind the gate it would answer 403 —
refusing to let someone out of a door they are already outside of. It revokes
whatever token it is given and is harmless with none.

Matching is exact for `/login` and `/logout`, prefix for `/static/`, and
pattern-based for the two credentialed routes. It is a literal list in one module, not a regex
assembled from route metadata, so reading it answers "what is public?"
completely.

Unauthenticated behaviour splits by method:

- Safe methods (GET, HEAD) → `303` to `/login?next=<path>`, so a bookmarked
  deep link survives the login.
- Everything else → `403`, with no redirect. A form post that silently became
  a login page would look to the operator like the action succeeded.

The `next` parameter is validated as a site-relative path before it is used in
a redirect. `renewal/web/calendar.py` already closed an open redirect once
(commit `298ac84`); this must not reopen the same hole. A `next` that is not a
single-slash-prefixed relative path is discarded in favour of `/`.

### Data

One migration, two tables.

```
app_user
  id             integer primary key
  email          text not null, unique on lower(email)
  password_hash  text not null
  display_name   text not null
  is_active      boolean not null default true
  failed_count   integer not null default 0
  locked_until   timestamptz null
  created_at     timestamptz not null default now()

user_session
  id             integer primary key
  token_sha256   text not null, unique, indexed
  user_id        integer not null references app_user(id) on delete cascade
  created_at     timestamptz not null default now()
  expires_at     timestamptz not null
  last_seen_at   timestamptz not null
```

The tables are `app_user` and `user_session` rather than `user` and
`session`. `user` is a reserved word in Postgres and would need quoting at
every mention, including in `conftest.py`'s `TRUNCATE` list; and a model class
named `Session` would shadow the SQLAlchemy `Session` that every web module
already imports. The model classes are `User` and `UserSession`.

Uniqueness on email is enforced on `lower(email)` so `Anne@` and `anne@`
cannot both exist; the address is stored as typed and compared lowercased.

The cookie carries a 32-byte `secrets.token_urlsafe` value. Only its SHA-256
is stored. A stolen database dump therefore yields no usable cookie, the same
reasoning that puts only a digest in `document.blob_sha256`'s neighbourhood
rather than the bytes. Lookup is one indexed read per request on an equality
predicate.

`is_active` exists so an account can be turned off without deleting rows that
a future attribution change will want to point at. Nothing writes it in this
change except the CLI.

### Passwords

`hashlib.scrypt` from the standard library. No new dependency, memory-hard,
and it fits a project whose dependency list is deliberately short. Per-user
random salt. The encoded hash string carries its own parameters
(`scrypt$n$r$p$salt$hash`) so cost can be raised later and old hashes still
verify; a hash that verifies under old parameters is rewritten at login.

Verification is constant-time via `hmac.compare_digest`. A login attempt for
an address with no account still performs a hash computation against a dummy
value, so response timing does not disclose which addresses exist.

### Sessions and the cookie

- `HttpOnly` — the session token is never readable by `app.js`.
- `SameSite=Lax` — blocks cross-site form posts while leaving ordinary
  top-level navigation into the app working.
- `Secure` — from a new `SESSION_COOKIE_SECURE` setting, default `true`.
  Local development over plain HTTP sets it false. It defaults to the safe
  value so that forgetting to configure it fails toward security.
- `Path=/`.

TTL from `SESSION_TTL_HOURS`, default 12 — about one working day, so the
operator logs in each morning rather than mid-task. `last_seen_at` slides the
expiry on use, so an active session does not expire out from under a long
review.

Logout deletes the session row. A logout that only cleared the cookie would
leave a token that still works if it was captured, which is the entire reason
the table exists rather than a signed cookie.

Expired rows are swept opportunistically on session creation. There is no
scheduled job: an expired row is already refused by the lookup, so the sweep
is housekeeping rather than a correctness requirement.

### CSRF

`SameSite=Lax` already prevents a cross-site form post from carrying the
cookie. On top of that, the same middleware checks `Origin` (falling back to
`Referer`) on every state-changing method and rejects a mismatch with `403`.

No per-form token. It would touch all thirteen templates and every `fetch` in
`app.js` for protection the two measures above already provide against the
threat this application actually faces.

The `Origin` check applies to allowlisted state-changing routes too, with one
exception: `POST /inbound/mail` is called by a machine that sends no `Origin`,
and it authenticates by signature. It is exempt, and the exemption is named in
the code rather than emerging from a missing-header default.

### Brute force

Ten consecutive failures locks the account for fifteen minutes;
`failed_count` resets on a successful login. A locked account returns the same
generic message as a wrong password, so the lockout itself discloses nothing
about whether an address exists.

### Module layout

A new `renewal/auth/` package holding pure functions over a SQLAlchemy
`Session`, with no FastAPI imports — the same shape as `renewal/carriers.py`
and `renewal/search/`, and testable without a web client:

- `renewal/auth/passwords.py` — `hash_password`, `verify_password`,
  `needs_rehash`
- `renewal/auth/sessions.py` — `create_session`, `lookup_session`,
  `revoke_session`, `sweep_expired`

The web layer is thin over that:

- `renewal/web/auth.py` — `GET/POST /login`, `POST /logout`
- the middleware, registered in `renewal/web/__init__.py`

`renewal.auth` must be added to `[tool.setuptools] packages` in
`pyproject.toml`. Commit `5dd79a0` fixed exactly this class of bug — a package
that exists in the tree, passes every test, and is missing from the wheel.

### Provisioning

```bash
python -m scripts.add_user anne@agency.com
```

Prompts twice for the password on the TTY via `getpass`, refuses a mismatch,
and enforces a 12-character minimum. Creates the account, or resets the
password if the address already exists, reporting which it did.

The password is never accepted as an argument. An argv password lands in shell
history and in the process table.

### UI

`renewal/templates/login.html` in the existing style, extending nothing that
requires a session. One email field, one password field, one error line that
says "Email or password is wrong" for every failure mode including lockout.

`base.html` gains the signed-in address and a logout button in the topbar.
`login.html` does not use `base.html`'s navigation, since none of those links
are reachable yet.

## Configuration

Two new settings on `Settings`, both with `.env.example` entries:

```
SESSION_COOKIE_SECURE=true   # false only for local HTTP development
SESSION_TTL_HOURS=12
```

## Testing

Unit, against a session with no web client:

- Hash and verify round-trip; a wrong password fails; two hashes of the same
  password differ (salting); `needs_rehash` is true for a lower-cost hash.
- Session create, lookup, expiry refusal, revoke, and the expired sweep.

Web, against `TestClient`:

- `GET /login` renders with no cookie.
- A wrong password, an unknown address, and a locked account all return the
  same message and set no cookie.
- A correct password sets an `HttpOnly` cookie and redirects.
- Logout deletes the row: the captured cookie is refused afterwards.
- A protected GET with no cookie redirects to `/login?next=` with the path.
- A protected POST with no cookie returns 403 and does not redirect.
- An expired session is refused.
- Each of the five public paths is reachable with no cookie.
- A `next` value pointing off-site is discarded rather than followed.
- A cross-site `Origin` on a state-changing request is rejected.
- `POST /inbound/mail` is not rejected for missing `Origin`.
- Ten failures lock; a correct password during the lock still fails; the
  counter resets after a successful login.

Packaging: extend `tests/test_packaging.py` so `renewal.auth` is asserted into
the wheel.

Existing web tests: `conftest.py`'s `TABLES` gains `app_user` and
`user_session`. A shared helper creates an account and returns an authenticated `TestClient`;
the `client_app` fixture in each `tests/test_web_*.py` calls it. Roughly ten
fixture edits, each two lines.

## Risks

**The webhook is still the weakest door.** `INBOUND_PROVIDER=filedrop`
verifies nothing, and `/inbound/mail` stays public by design. On a
network-reachable install with the filedrop provider configured, anyone who
can reach the route can post mail into the record as any carrier. Auth does
not close this, and the existing warning in `.env.example` should be
sharpened rather than assumed to be read.

**The ics token is unchanged.** Anyone holding the feed URL reads every client
name and deadline without logging in. That was already true and is already
said in words on the settings page. Adding auth may make it *feel* addressed
when it is not.

**Sessions are not scoped to anything.** Every account sees the whole book.
That is correct for one agency and would be wrong the moment a second one
exists.
