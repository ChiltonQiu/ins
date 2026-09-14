"""One sheet to read while the phone is ringing.

It computes nothing and calls no model. Every number on it already exists in a
row that something else wrote, and this module only chooses which of them to
put next to each other.

That restraint is the point. A generated paragraph she reads to a client over
the phone is the one place in this system where a hallucination reaches a
client with no document, no draft and no second look in between. The rules it
follows are inherited rather than invented: an unconfirmed date says so, a
derived date shows its arithmetic, unknown prints as the word, and the
residual is stated as unattributable and never as a cause.

Nothing on the sheet is a recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.calendarview.agenda import AgendaEntry
from renewal.clients.overview import (
    RENEWAL_WINDOW_DAYS,
    ClientOverview,
    PolicyRow,
    overview,
)
from renewal.comparison import matrix_for
from renewal.models import AttentionItem, Client, Comparison, ComparisonColumn
from renewal.models import InboundMessage, PolicyTerm

# What the residual is, said once. It is the part of the premium move these
# documents do not account for — not a cause, and not a carrier's reasoning.
RESIDUAL_LABEL = "not attributable from these documents"


@dataclass(frozen=True)
class RenewalRow:
    policy: PolicyRow
    comparison_id: int | None
    total_delta: Decimal | None
    residual: Decimal | None
    material_changes: list[str]
    # Set when there is no comparison yet: the picker, with nothing chosen.
    compare_url: str | None

    @property
    def days_to_renewal(self) -> int | None:
        """The one number she says out loud first, so it is on the row rather
        than a level down."""
        return self.policy.days_to_renewal


@dataclass(frozen=True)
class CallPrep:
    client: Client
    renewals: list[RenewalRow]
    other_policies: list[PolicyRow]
    upcoming_dates: list[AgendaEntry]
    attention: list[AttentionItem]
    messages: list[InboundMessage]


def _latest_comparison(session: Session, policy_id: int) -> Comparison | None:
    """The newest comparison any of this policy's terms is a column of.

    Read off comparison_column rather than off the comparison, because that is
    where the terms actually live; a comparison written before the matrix has
    no columns and is not offered here.
    """
    return session.scalar(
        select(Comparison)
        .join(ComparisonColumn, ComparisonColumn.comparison_id == Comparison.id)
        .join(PolicyTerm, PolicyTerm.id == ComparisonColumn.policy_term_id)
        .where(PolicyTerm.policy_id == policy_id)
        .order_by(Comparison.id.desc())
        .limit(1)
    )


def _describe(field_path: str, baseline: str | None, value: str | None) -> str:
    """The row as words, with both values and no interpretation of either.

    A value missing from one document is said to be missing from that
    document, never assumed to be a removal.
    """
    before = baseline if baseline is not None else "not on this document"
    after = value if value is not None else "not on this document"
    return f"{field_path}: {before} → {after}"


def _renewal_row(session: Session, policy: PolicyRow, *, settings) -> RenewalRow:
    comparison = _latest_comparison(session, policy.policy_id)
    if comparison is None:
        # Rather than comparing on the spot, which would be a model call
        # inside a page load she opened while the phone was ringing.
        return RenewalRow(
            policy=policy,
            comparison_id=None,
            total_delta=None,
            residual=None,
            material_changes=[],
            compare_url=f"/policies/{policy.policy_id}/compare",
        )

    matrix = matrix_for(session, comparison, include_noise=False, settings=settings)
    comparand = next(
        (column for column in matrix.columns if column.role == "comparand"), None
    )
    breakdown = comparand.breakdown if comparand else None
    return RenewalRow(
        policy=policy,
        comparison_id=comparison.id,
        total_delta=comparand.total_delta if comparand else None,
        residual=breakdown.residual if breakdown and breakdown.available else None,
        material_changes=[
            _describe(
                row.difference.field_path,
                row.baseline.value,
                row.comparands[0].value if row.comparands else None,
            )
            for row in matrix.rows
            if row.difference.materiality == "material"
        ],
        compare_url=None,
    )


def prep(
    session: Session,
    client_id: int,
    *,
    agency_id: int,
    today: date | None = None,
    settings=None,
) -> CallPrep:
    """A second view over overview(), not a second assembly.

    Sharing the query means the sheet and the client page cannot disagree
    about what is renewing or what needs attention.
    """
    got: ClientOverview = overview(session, client_id, agency_id=agency_id, today=today)
    renewing = [
        policy
        for policy in got.policies
        if policy.days_to_renewal is not None
        and policy.days_to_renewal <= RENEWAL_WINDOW_DAYS
    ]
    renewing_ids = {policy.policy_id for policy in renewing}
    return CallPrep(
        client=got.client,
        renewals=[
            _renewal_row(session, policy, settings=settings) for policy in renewing
        ],
        other_policies=[
            policy for policy in got.policies if policy.policy_id not in renewing_ids
        ],
        upcoming_dates=got.upcoming_dates,
        attention=got.attention,
        messages=got.messages,
    )
