"""Draft generation.

The output is a draft for a licensed human to read and edit, never a message to
a client. It explains what changed; it does not advise, because advising is
licensed activity and not the model's job.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from renewal.config import Settings
from renewal.models import Draft
from renewal.providers import text_block

SYSTEM = """You write short, plain-English notes that an insurance agent sends
to a client explaining what changed at renewal.

Rules:
- Explain what changed. Do not recommend anything, do not advise the client what
  to do, and do not comment on whether their coverage is adequate.
- No jargon. Write "the amount you pay before insurance starts covering a claim"
  rather than "deductible" only if the plain phrasing is clearer; otherwise use
  the ordinary word.
- Use only the facts given below. Do not infer causes. If part of the premium
  change is listed as not attributable, say plainly that the documents do not
  break that part down.
- Under 200 words. No greeting, no signature.
"""


INSTRUCTIONS = (
    "Write a short, plain-English note under 200 words explaining these changes "
    "to the client. Do not recommend any action and do not advise the client "
    "what to do — only explain what changed. No greeting, no signature."
)


def build_prompt(matrix) -> str:
    """Material and informational differences only. Noise never reaches the
    model.

    Two columns by construction: generate_draft is only called for a
    draft-eligible matrix, which is two bound terms of one policy.
    """
    lines = ["Changes at renewal:"]
    for row in matrix.rows:
        if row.difference.materiality == "noise":
            continue
        before = row.baseline.value if row.baseline.value is not None else "(absent)"
        after = (
            row.comparands[0].value
            if row.comparands[0].value is not None
            else "(absent)"
        )
        lines.append(
            f"- [{row.difference.materiality}] {row.difference.field_path}: "
            f"{before} -> {after}"
        )

    breakdown = matrix.columns[1].breakdown
    lines.append("")
    if breakdown is None or not breakdown.available:
        reason = breakdown.reason if breakdown else matrix.columns[1].breakdown_reason
        lines.append(
            f"Premium change cannot be broken down: {reason}. Say this "
            "plainly rather than speculating."
        )
        lines.append("")
        lines.append(INSTRUCTIONS)
        return "\n".join(lines)

    lines.append(f"Total premium change: {breakdown.total_delta}")
    for attribution in breakdown.lines:
        lines.append(f"  {attribution.amount} from {attribution.label}")
    lines.append(
        f"  {breakdown.residual} is not attributable from these documents. Say so; "
        "do not guess at a cause such as a rate increase."
    )
    lines.append("")
    lines.append(INSTRUCTIONS)
    return "\n".join(lines)


def generate_draft(
    session: Session,
    matrix,
    *,
    client,
    settings: Settings,
) -> Draft:
    text = client.complete(
        model=settings.draft_model,
        system=SYSTEM,
        content=[text_block(build_prompt(matrix))],
    )
    draft = Draft(comparison_id=matrix.comparison.id, generated_text=text)
    session.add(draft)
    session.flush()
    return draft


def save_edit(session: Session, draft: Draft, final_text: str) -> Draft:
    """An edit is a new row carrying the original generated text. Latest wins."""
    edited = Draft(
        comparison_id=draft.comparison_id,
        generated_text=draft.generated_text,
        final_text=final_text,
        edited_at=dt.datetime.now(dt.timezone.utc),
    )
    session.add(edited)
    session.flush()
    return edited


def latest_draft(session: Session, comparison_id: int) -> Draft | None:
    return (
        session.query(Draft)
        .filter_by(comparison_id=comparison_id)
        .order_by(Draft.id.desc())
        .first()
    )
