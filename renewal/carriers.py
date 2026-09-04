"""Resolving a free-text carrier name to a carrier, and reading its admitted
status.

Exact match only, on the normalized display name or a normalized alias. Fuzzy
carrier matching is deliberately absent: picking the wrong company would be
invisible, and the value it feeds — admitted versus non-admitted — changes how
a policy should be read.

Admitted status is per state because the same carrier can be admitted in one
and surplus-lines in another. A state with no recorded status is unknown, and
so is a policy with no state. Neither is inferred.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.models import Carrier, CarrierAdmittedStatus, CarrierAlias, Policy


def normalize_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def resolve_carrier(session: Session, name: str) -> Carrier | None:
    target = normalize_name(name)
    for carrier in session.scalars(select(Carrier)):
        if normalize_name(carrier.display_name) == target:
            return carrier
    alias = session.scalar(
        select(CarrierAlias).where(CarrierAlias.alias == target)
    )
    return session.get(Carrier, alias.carrier_id) if alias else None


def add_alias(session: Session, carrier_id: int, name: str) -> CarrierAlias:
    alias = CarrierAlias(carrier_id=carrier_id, alias=normalize_name(name))
    session.add(alias)
    session.flush()
    return alias


def set_admitted(
    session: Session, carrier_id: int, state: str, status: str,
    *, set_by: str = "human",
) -> CarrierAdmittedStatus:
    row = CarrierAdmittedStatus(
        carrier_id=carrier_id, state=state.upper(), status=status, set_by=set_by
    )
    session.add(row)
    session.flush()
    return row


def admitted_status(session: Session, carrier_id: int, state: str | None) -> str:
    if not state:
        return "unknown"
    return session.scalar(
        select(CarrierAdmittedStatus.status)
        .where(CarrierAdmittedStatus.carrier_id == carrier_id)
        .where(CarrierAdmittedStatus.state == state.upper())
        .order_by(CarrierAdmittedStatus.id.desc())
        .limit(1)
    ) or "unknown"


def unresolved_carrier_names(session: Session) -> list[str]:
    """Names on policies that resolve to no carrier. Surfaced so she can alias
    or create them, rather than created automatically."""
    names = session.scalars(select(Policy.carrier_name).distinct())
    return sorted(n for n in names if n and resolve_carrier(session, n) is None)
