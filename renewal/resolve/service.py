"""Attaching a document to a client.

Auto-linking requires exactly one candidate at the threshold, which only an
exact policy-number match reaches. Everything else — no match, a name-only
match, or two equally exact matches — is left unlinked and appears in the
queue. A document with no link row is unmatched; there is no status column to
disagree with reality.

Her manual assignment inserts a new link carrying the ranked list that was
shown. That row, beside the auto row it supersedes, is the Correction
equivalent: these were offered, this was right.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.models import Document, DocumentLink
from renewal.resolve.candidates import extract_candidates
from renewal.resolve.matching import Match, rank_matches
from renewal.text.store import page_text

logger = logging.getLogger(__name__)

AUTO_LINK_THRESHOLD = 1.0


def latest_link(session: Session, document_id: int) -> DocumentLink | None:
    return session.scalar(
        select(DocumentLink)
        .where(DocumentLink.document_id == document_id)
        .order_by(DocumentLink.id.desc())
        .limit(1)
    )


def unmatched(session: Session, *, limit: int = 50) -> list[Document]:
    linked = select(DocumentLink.document_id).distinct()
    return list(
        session.scalars(
            select(Document)
            .where(Document.id.not_in(linked))
            .order_by(Document.uploaded_at.desc())
            .limit(limit)
        )
    )


def candidates_for(session: Session, document: Document) -> list[Match]:
    return rank_matches(session, extract_candidates(page_text(session, document.id, 1)))


def resolve_document(session: Session, document: Document) -> DocumentLink | None:
    existing = latest_link(session, document.id)
    if existing is not None:
        # Already linked, by the pipeline stage or by her. Resolution never
        # runs twice over the same document and never overrides a decision.
        return existing
    matches = candidates_for(session, document)
    exact = [m for m in matches if m.score >= AUTO_LINK_THRESHOLD]
    if len(exact) != 1:
        # Ambiguous or absent. Queued for a human, never guessed.
        logger.info(
            "document unmatched document_id=%s candidates=%s",
            document.id, len(matches),
        )
        return None
    match = exact[0]
    link = DocumentLink(
        document_id=document.id, client_id=match.client_id,
        policy_id=match.policy_id, method="auto", confidence=match.score,
        candidates=[asdict(m) for m in matches],
    )
    session.add(link)
    session.flush()
    logger.info(
        "document auto-linked document_id=%s client_id=%s policy_id=%s",
        document.id, match.client_id, match.policy_id,
    )
    return link


def assign(
    session: Session, document_id: int, *, client_id: int,
    policy_id: int | None, candidates: list[dict],
) -> DocumentLink:
    link = DocumentLink(
        document_id=document_id, client_id=client_id, policy_id=policy_id,
        method="manual", confidence=1.0, candidates=candidates,
    )
    session.add(link)
    session.flush()
    return link
