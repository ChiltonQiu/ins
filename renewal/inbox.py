"""What became of a document.

Nothing here writes. Every bucket is computed from rows the pipeline already
wrote, on every read, because the state changes the moment she acts on it — a
stored copy would be stale immediately after the act that fixed it, and the one
thing worse than no status is a status that lies.

The single exception is Document.status, which records that work is in flight.
That has nothing to derive from: an unprocessed document and a document being
processed right now look identical in every other table.

Order matters below. The checks run from "we know least" to "we know most", so
the reason on the row is the earliest thing that stopped it rather than the
last thing that noticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.classify.runner import latest_class
from renewal.corrections import effective_values
from renewal.models import (
    ComparisonColumn, Document, Extraction, Policy, PolicyTerm,
)
from renewal.pipeline import should_extract_fields
from renewal.promote import unresolved_field_paths
from renewal.resolve.service import latest_link

BUCKETS = ("working", "stalled", "needs_you", "done")

REASONS = (
    "failed",
    "needs_client",
    "needs_policy",
    "needs_review",
    "nothing_extracted",
)

# Long enough that a slow provider call is not mistaken for a dead process,
# short enough that she is not staring at a spinner for an afternoon. OCR plus
# three model calls is the worst realistic case and runs well under this.
STALLED_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class DocumentState:
    document: Document
    bucket: str
    reason: str | None
    summary: str
    client_id: int | None
    policy_id: int | None
    comparison_id: int | None
    extraction_id: int | None


def _latest_extraction(session: Session, document_id: int) -> Extraction | None:
    return (
        session.query(Extraction)
        .filter_by(document_id=document_id)
        .order_by(Extraction.id.desc())
        .first()
    )


def _comparison_for(session: Session, term_id: int) -> int | None:
    """The newest comparison this term appears in, whichever column it is."""
    return session.scalar(
        select(ComparisonColumn.comparison_id)
        .where(ComparisonColumn.policy_term_id == term_id)
        .order_by(ComparisonColumn.comparison_id.desc())
        .limit(1)
    )


def _state(
    document: Document,
    bucket: str,
    summary: str,
    *,
    reason: str | None = None,
    client_id: int | None = None,
    policy_id: int | None = None,
    comparison_id: int | None = None,
    extraction_id: int | None = None,
) -> DocumentState:
    return DocumentState(
        document=document,
        bucket=bucket,
        reason=reason,
        summary=summary,
        client_id=client_id,
        policy_id=policy_id,
        comparison_id=comparison_id,
        extraction_id=extraction_id,
    )


def state_of(
    session: Session,
    document: Document,
    *,
    stalled_after: timedelta = STALLED_AFTER,
) -> DocumentState:
    if document.status == "processing":
        changed = document.status_changed_at
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - changed > stalled_after:
            return _state(
                document, "stalled",
                "Started but never finished — the application restarted while "
                "this was in flight.",
            )
        return _state(document, "working", "Reading this now.")

    if document.status == "failed":
        return _state(
            document, "needs_you", "Processing failed. Retry, or open it to "
            "enter the fields by hand.",
            reason="failed",
        )

    link = latest_link(session, document.id)
    if link is None:
        return _state(
            document, "needs_you", "Not filed yet — who is this for?",
            reason="needs_client",
        )

    # Before the policy check on purpose. A document that never routes to field
    # extraction is not waiting for a policy: no term was ever expected of it,
    # and asking her to attach an invoice to a policy is asking for a decision
    # that means nothing.
    if not should_extract_fields(session, document.id):
        label = latest_class(session, document.id) or "unclassified"
        return _state(
            document, "done",
            f"Stored and searchable — {label.replace('_', ' ')}, "
            "no policy term expected.",
            client_id=link.client_id, policy_id=link.policy_id,
        )

    if link.policy_id is None:
        return _state(
            document, "needs_you", "Filed to a client — which policy?",
            reason="needs_policy", client_id=link.client_id,
        )

    extraction = _latest_extraction(session, document.id)
    if extraction is None:
        # The bulk-import default: routed, but extraction was switched off for
        # this document on purpose. Not-yet-processed is not stuck, and putting
        # an entire imported archive in the queue would bury the real rows.
        return _state(
            document, "done",
            "Stored and searchable — fields were not extracted.",
            client_id=link.client_id, policy_id=link.policy_id,
        )

    if unresolved_field_paths(session, extraction.id):
        return _state(
            document, "needs_you", "Some fields need checking before this can "
            "be filed.",
            reason="needs_review", client_id=link.client_id,
            policy_id=link.policy_id, extraction_id=extraction.id,
        )

    if not effective_values(session, extraction.id):
        return _state(
            document, "needs_you", "Nothing could be read from this.",
            reason="nothing_extracted", client_id=link.client_id,
            policy_id=link.policy_id, extraction_id=extraction.id,
        )

    term = (
        session.query(PolicyTerm)
        .filter_by(promoted_from_extraction_id=extraction.id)
        .first()
    )
    if term is None:
        # Clean, linked, and still not promoted: the twin check stopped it,
        # because these exact bytes are already filed against this policy. A
        # resend is not a problem and must not read like one.
        return _state(
            document, "done",
            "Already filed — an identical document is on this policy.",
            client_id=link.client_id, policy_id=link.policy_id,
            extraction_id=extraction.id,
        )

    policy = session.get(Policy, term.policy_id)
    number = policy.policy_number if policy is not None else "this policy"
    comparison_id = _comparison_for(session, term.id)
    if comparison_id is not None:
        summary = f"Renewal term on {number} — compared with the prior term."
    else:
        summary = f"Term on {number} — filed."
    return _state(
        document, "done", summary,
        client_id=link.client_id, policy_id=link.policy_id,
        comparison_id=comparison_id, extraction_id=extraction.id,
    )


def inbox_rows(
    session: Session,
    *,
    stalled_after: timedelta = STALLED_AFTER,
    limit: int = 200,
) -> list[DocumentState]:
    """Every document, newest first, with what became of it.

    Capped rather than paged: past a couple of hundred rows the inbox is not
    the right screen any more and search is. The cap is here so a bulk-imported
    archive cannot turn the front door into a several-second query.
    """
    documents = session.scalars(
        select(Document).order_by(Document.id.desc()).limit(limit)
    )
    return [
        state_of(session, document, stalled_after=stalled_after)
        for document in documents
    ]


def bucketed(states: list[DocumentState]) -> dict[str, list[DocumentState]]:
    """Three buckets on the page, four states underneath.

    Stalled is not its own bucket: it is the same answer to "is this finished?"
    as working on it, with a different explanation and a button. Sorting it to
    the top of that bucket puts the one row that needs her first.
    """
    groups: dict[str, list[DocumentState]] = {
        "working": [], "needs_you": [], "done": [],
    }
    for state in states:
        key = "working" if state.bucket in ("working", "stalled") else state.bucket
        groups[key].append(state)
    groups["working"].sort(key=lambda s: s.bucket != "stalled")
    return groups


def needs_you_count(session: Session, *, limit: int = 200) -> int:
    """The number beside the nav link, and what the notification email counts.

    Computed the same way the page computes it, in one place, so the badge and
    the list cannot disagree.
    """
    return sum(
        1 for state in inbox_rows(session, limit=limit)
        if state.bucket == "needs_you"
    )


def anything_in_flight(session: Session) -> bool:
    """Whether the page should keep polling."""
    return session.scalar(
        select(Document.id).where(Document.status == "processing").limit(1)
    ) is not None
