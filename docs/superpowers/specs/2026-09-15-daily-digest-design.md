# The Daily Summary — Design

Date: 2026-09-15
Status: approved for planning
Scope: give the application a clock of its own, so that what it knows reaches
her without her having to come and look.

## Context

Everything this application says is edge-triggered by mail arriving.

`maybe_notify` is called from exactly one place — `renewal/background.py:113`,
at the end of processing one document — so an inbound document is the only
event in the system that can cause an outbound word. The badge on the nav is
computed per request (`renewal/web/navbadge.py:40`). The one time-based
attention rule, `unconfirmed_date_soon`, is computed at read time
(`renewal/attention/rules.py:196`) and `attention/rules.py:13` says why: it
changes with the clock, and materialising it would need a scheduler this design
does not have.

Each of those decisions is right on its own. Together they add up to a system
that only speaks when spoken to, and the gap they leave is exact:

> In a week where no mail arrives, the app is silent — including about a
> deadline next Tuesday.

Making the window configurable, which the settings work of 2026-09-15 did, does
not touch this. It changes what she sees when she looks. It does not change
whether she looks.

### Two failures, not one

The deadline is the obvious half. The other half is worse and cheaper to fix:

A quiet week and a mail webhook that has been failing since Thursday are, from
where she sits, the same thing. Both are an application that says nothing. The
only system that could notice the difference is this one, and it currently has
no way to mention it.

### Why not just a scheduler

Because the thing needed here is not a scheduler. A scheduler fires an event at
a time; what is missing is a *question asked repeatedly* — given the state of
the world right now, should she hear from me today? — and the answer computed
from state rather than from events.

That distinction is what makes an unreliable clock good enough. If the answer
comes from state, a tick that never happened costs nothing: the next tick asks
the same question and gets the same answer, because the deadline is still next
Tuesday. Missing 08:00 is not missing the event; there is no event.

## Decisions

1. **The summary is computed from state, on every tick.** No queue of pending
   notifications, no rows written ahead of time, nothing to drift. The same
   read-time functions the pages use — `inbox.needs_you_count`,
   `attention.rules.open_items`, `calendarview.agenda.agenda` — are the only
   sources, so the email and the screens cannot disagree. This is the rule
   `renewal/notify.py:52` already states, extended to everything the email says.
2. **One summary per local day, and the database is what remembers.**
   `notification_send` gains a `digest_date`, unique where it is set. After the
   first successful send, every later tick that day finds the row and stops.
3. **It fails toward noise, never toward silence.** The row is written *after*
   the send, keeping the existing contract that nothing may claim a send that
   did not happen (`renewal/notify.py:86`). Two processes racing the same
   minute therefore send two emails rather than none. That is the correct way
   round: this is the same bias `attention/rules.py:11` states, and a design
   that claims the day *before* sending trades one duplicate email for a silent
   missed day, which is the bug being fixed.
4. **The clock is a daemon thread in the application process.** Same size as
   `background.py`: no worker, no queue, no second service to deploy or
   monitor. It ticks every five minutes and asks the question.
5. **Startup is a tick.** A process that was down at 08:00 and comes up at
   14:00 sends at 14:00. Only a day the application is down for its entirety is
   a day with no summary — and the next day's summary still names the same
   deadline, because it is computed from state.
6. **Silence is itself reported.** When nothing needs her, the summary is not
   sent — except that after `digest_quiet_days` with no summary at all, one
   goes out saying so. An application that speaks only when it has news cannot
   be told apart from one that has died.
7. **The email says numbers, a date, and a link.** No client names, no policy
   numbers, no document filenames. `renewal/notify.py:78` is unchanged law: the
   login exists to keep client detail off a mail server, and a summary is not
   an exception to it. A bare date carries no client detail and stays.
8. **The event email and the daily summary share one body.** They are one
   question asked by two clocks, not two notification systems. The email that
   fires when a document finishes processing gains the deadline line for free,
   which is most of this gap closed even for the weeks when mail does arrive.

## Design

### What a summary is

```python
@dataclass(frozen=True)
class Digest:
    needs_you: int          # inbox.needs_you_count
    attention: int          # len(open_items)
    upcoming: int           # dates within the window, today forward
    soonest: date | None    # the nearest of those dates
    quiet_days: int | None  # days since the last document arrived, or None
```

`collect(session, *, settings, today)` builds it from the three read-time
functions. `is_quiet` is the property that every count is zero.

`render(digest, *, settings, quiet)` returns `(subject, body)`:

```
3 documents need you.
2 items in the attention queue.
2 dates in the next 14 days — the soonest is Tue 22 Sep.

Nothing has arrived in 6 days.

http://127.0.0.1:8000/
```

Every line whose number is zero is left out. The arrival line appears only
after `digest_quiet_days`, where it is the whole point of the email.

The quiet subject is "Nothing needs you" — said plainly, because an email that
looks like an alert and contains no alert trains her to stop opening them.

### When it sends

`send_due_digest(session, *, settings, now, send=None) -> bool`, which is the
whole decision in one place and is the only thing the clock calls:

1. Off unless `digest_enabled`, `smtp_host` and `notify_to` are all set — the
   same three-way check `maybe_notify` already makes, and for the same reasons.
2. Local date and hour come from `AGENCY_TZ`. Before `digest_hour`, stop.
3. A `notification_send` row for today's local date already exists: stop.
4. Collect. If it is not quiet, send. If it is quiet, send only when the last
   summary of any kind was `digest_quiet_days` or longer ago.
5. On a successful send, write the row with `digest_date` set. On a failed
   send, write nothing and log — the next tick tries again, unchanged from
   `renewal/notify.py:86`.

A row with `digest_date` set is a summary; a row with it NULL is the existing
event-triggered email. Postgres treats NULLs as distinct, so the two live in
one table without interfering — the same property `uq_inbound_message_id`
already depends on (`renewal/models.py:630`).

### The clock

`DigestClock(session_factory, settings, *, tick_seconds, sleep=time.sleep)`.
`tick()` is one pass and is what the tests drive; `start()` puts `_loop` on a
daemon thread. Injecting `sleep` is what lets a test prove the loop ticks
without a test that sleeps — which on a slow machine is a test that fails.

It is started from `renewal/app.py`, the production wiring, and not from
`create_app`. A hundred test applications must not each raise a thread, and the
factory should stay a pure function of its arguments.

Failure is contained the way the badge middleware contains it
(`renewal/web/navbadge.py:44`): a tick that raises is logged and the loop
continues. A clock that dies of one bad night's data is a clock that is silent
forever after, which is this bug again.

`scripts/digest.py` runs exactly one tick and exits, so an installation that
would rather drive this from cron or a systemd timer can, with the same code
and the same once-a-day guarantee. It is an option at deploy time rather than a
second architecture.

### What is configurable, and where

Preferences, on `/settings` — judgments about how she wants to work:

| Setting | Default | Range |
|---|---|---|
| `digest_enabled` | true | — |
| `digest_hour` | 8 | 0–23 |
| `digest_quiet_days` | 7 | 1–30 |

Deployment, in the environment — set once against the machine:

| Variable | Default |
|---|---|
| `AGENCY_TZ` | `UTC` |
| `DIGEST_TICK_SECONDS` | `300` |

`AGENCY_TZ` is new and it matters: every timestamp in this application is UTC
today, so without it "eight in the morning" is four in the morning on the east
coast. It is deployment configuration by the rule `settings_store.py:9` sets —
set once, against the machine, never a judgment about insurance.

## What this does not do

- **It does not make anything happen.** The summary counts and links. No draft
  is sent, no comparison is built, nothing auto-resolves. `attention/rules.py:6`
  is unchanged.
- **It does not add a second process.** A cron entry is offered, not required.
- **It does not survive the application being down.** Nothing in-process can.
  The failure is visible the same way a stalled document is: the summary simply
  does not arrive, and the next one names the same deadline.
- **It does not put client names in mail.** Deliberately, permanently.
- **It does not notify per deadline.** One email a day carrying a count and the
  nearest date, not one per date. Per-date alarms belong in her calendar, which
  is a scheduler she already has and already checks — a `VALARM` in the `.ics`
  feed is the natural next step and is deliberately out of scope here, because
  Google Calendar ignores alarms on subscribed feeds and a channel that works
  for some clients and not others must not be the one carrying the deadline.
