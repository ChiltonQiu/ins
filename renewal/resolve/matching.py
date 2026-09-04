"""Ranked candidates with scores. Never a single silent answer.

Only an exact policy-number match reaches 1.0, which is the auto-link
threshold. Name similarity is offered and scored but is capped below it on
purpose: name-similarity auto-linking is exactly where misfiling happens, and a
cancellation notice filed under the wrong client is the worst outcome this
system can produce.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from renewal.models import Client, Policy
from renewal.resolve.candidates import Candidates

NAME_SIMILARITY_FLOOR = 0.3
NAME_SCORE_CAP = 0.95


@dataclass(frozen=True)
class Match:
    client_id: int
    policy_id: int | None
    score: float
    reason: str


def _normalize(value: str) -> str:
    return " ".join(value.split()).upper()


def rank_matches(
    session: Session, candidates: Candidates, *, limit: int = 5
) -> list[Match]:
    matches: dict[tuple[int, int | None], Match] = {}

    for number in candidates.policy_numbers:
        rows = session.execute(
            select(Policy.id, Policy.client_id).where(
                func.upper(func.trim(Policy.policy_number)) == _normalize(number)
            )
        ).all()
        for policy_id, client_id in rows:
            matches[(client_id, policy_id)] = Match(
                client_id=client_id, policy_id=policy_id, score=1.0,
                reason="exact policy number",
            )

    if candidates.named_insured:
        similarity = func.similarity(Client.display_name, candidates.named_insured)
        rows = session.execute(
            select(Client.id, similarity)
            .where(similarity > NAME_SIMILARITY_FLOOR)
            .order_by(similarity.desc())
            .limit(limit)
        ).all()
        for client_id, score in rows:
            key = (client_id, None)
            if key in matches:
                continue
            matches[key] = Match(
                client_id=client_id, policy_id=None,
                score=min(float(score), NAME_SCORE_CAP),
                reason="named insured similarity",
            )

    return sorted(matches.values(), key=lambda m: -m.score)[:limit]
