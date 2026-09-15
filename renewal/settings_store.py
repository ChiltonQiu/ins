"""The settings an operator can change, and what they are allowed to be.

Two audiences, split on purpose.

Everything here is a judgment about how the agency wants to work — how eager
the alerts are, who gets told, how long a session lasts. She changes these from
/settings and they take effect on the next read, with no restart.

Everything NOT here is deployment configuration and lives in the environment:
model names, SMTP host and credentials, worker counts, poll intervals. Those
are set once against the machine, and putting them on a screen would only
invite someone to change them without a reason to.

And some things are configurable nowhere at all. AUTO_LINK_THRESHOLD is D8: a
document auto-files only on an exact policy-number match. Expose it as a knob
and a near-miss files one client's renewal onto another client's policy while
reporting success, which is worse than not filing it. The scrypt parameters and
the prompt versions are correctness too, not preference.

The layering: read() returns what she has stored, effective() returns a frozen
Settings with those values over the environment's. Call sites take a Settings
and never learn that any of this happened.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.models import AgencySetting

AGENCY_ID = 1

# Deliberately loose. The job is to catch a typo like a missing @, not to
# adjudicate RFC 5322 — which no regex does anyway, and a false refusal here
# means she cannot turn her own notifications on.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class Invalid(ValueError):
    """A value the form must refuse, carrying the reason she should read."""


@dataclass(frozen=True)
class Definition:
    key: str
    label: str
    help: str
    kind: str  # 'bool' | 'int' | 'float' | 'email'
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""


DEFINITIONS: tuple[Definition, ...] = (
    Definition(
        key="notify_enabled",
        label="Email me when documents need me",
        help=(
            "Off means the inbox badge is the only signal. Turning this off "
            "keeps the address below, so you can turn it back on without "
            "retyping it."
        ),
        kind="bool",
    ),
    Definition(
        key="notify_to",
        label="Send those emails to",
        help="Leave empty to send none. Only ever one address.",
        kind="email",
    ),
    Definition(
        key="notify_min_interval_minutes",
        label="At most one email every",
        help=(
            "A bulk import that strands twenty documents sends one email, not "
            "twenty. The email always lists everything waiting, not just what "
            "triggered it."
        ),
        kind="int",
        minimum=1,
        maximum=1440,
        unit="minutes",
    ),
    Definition(
        key="attention_premium_pct",
        label="Flag a premium change over",
        help=(
            "Raised when a renewal term is compared with the prior one. Lower "
            "is noisier; a wrong flag costs one keystroke to dismiss and a "
            "missed increase costs a phone call."
        ),
        kind="float",
        minimum=0,
        maximum=100,
        unit="%",
    ),
    Definition(
        key="unconfirmed_date_window_days",
        label="Warn about unconfirmed dates within",
        help=(
            "An extracted date nobody has confirmed or dismissed shows in the "
            "attention queue this far ahead. It is computed when you open the "
            "page, so changing this changes the list immediately."
        ),
        kind="int",
        minimum=1,
        maximum=365,
        unit="days",
    ),
    Definition(
        key="session_ttl_hours",
        label="Sign me out after",
        help=(
            "An active session slides, so a long review will not expire "
            "underneath you. This is the idle limit."
        ),
        kind="int",
        minimum=1,
        maximum=720,
        unit="hours",
    ),
)

BY_KEY = {definition.key: definition for definition in DEFINITIONS}


def parse(definition: Definition, raw: str) -> Any:
    """Text off a form into the typed value, or Invalid with the reason."""
    raw = (raw or "").strip()

    if definition.kind == "bool":
        return raw.lower() in ("1", "true", "yes", "on")

    if definition.kind == "email":
        if raw and not _EMAIL.match(raw):
            raise Invalid(f"{definition.label}: that is not an email address")
        return raw

    try:
        value = int(raw) if definition.kind == "int" else float(raw)
    except ValueError:
        raise Invalid(f"{definition.label}: needs a number") from None

    if definition.minimum is not None and value < definition.minimum:
        raise Invalid(
            f"{definition.label}: cannot be below "
            f"{_plain(definition.minimum)}{definition.unit and ' ' + definition.unit}"
        )
    if definition.maximum is not None and value > definition.maximum:
        raise Invalid(
            f"{definition.label}: cannot be above "
            f"{_plain(definition.maximum)}{definition.unit and ' ' + definition.unit}"
        )
    return value


def _plain(number: float) -> str:
    return str(int(number)) if number == int(number) else str(number)


def read(session: Session, *, agency_id: int = AGENCY_ID) -> dict[str, Any]:
    """The operator's stored values, newest row per key.

    Rows are appended, so this reads the latest and ignores the history. An
    unparseable stored value is skipped rather than raised on: a bad row must
    not be able to take every page down, and the environment default behind it
    is always a working value.
    """
    rows = session.execute(
        select(AgencySetting.key, AgencySetting.value)
        .where(AgencySetting.agency_id == agency_id)
        .order_by(AgencySetting.id.desc())
    ).all()

    out: dict[str, Any] = {}
    for key, value in rows:
        if key in out or key not in BY_KEY:
            continue  # an older row for a key already seen, or a retired key
        try:
            out[key] = parse(BY_KEY[key], value)
        except Invalid:
            continue
    return out


def write(
    session: Session, key: str, raw: str, *, agency_id: int = AGENCY_ID
) -> Any:
    """Append one setting. Returns the parsed value it stored."""
    definition = BY_KEY.get(key)
    if definition is None:
        raise Invalid(f"no such setting: {key}")
    value = parse(definition, raw)
    session.add(
        AgencySetting(
            agency_id=agency_id,
            key=key,
            # Normalised on the way in, so what comes back out of read() is
            # what parse() produced rather than whatever the form spelled.
            value=str(value).lower() if definition.kind == "bool" else str(value),
        )
    )
    session.flush()
    return value


def effective(
    session: Session, settings: Settings, *, agency_id: int = AGENCY_ID
) -> Settings:
    """Her settings layered over the environment's.

    A new frozen Settings, so every existing call site keeps taking the one
    object it always took and never learns this happened.
    """
    stored = read(session, agency_id=agency_id)
    return dataclasses.replace(settings, **stored) if stored else settings
