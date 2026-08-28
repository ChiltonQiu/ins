"""Assembling a comparison from two frozen terms.

The comparison and its differences are written once. Reclassifying in the UI
logs a reclassification row rather than editing the difference — that log is the
evidence for which rules are wrong.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from renewal.diff import diff_terms, term_field_map
from renewal.materiality import RuleSet, classify
from renewal.models import Comparison, Difference, PolicyTerm, Reclassification
from renewal.premium import PremiumBreakdown, attribute_premium


def build_comparison(
    session: Session,
    *,
    run_id: int,
    prior_term: PolicyTerm,
    renewal_term: PolicyTerm,
    rules: RuleSet,
) -> Comparison:
    comparison = Comparison(
        renewal_run_id=run_id,
        prior_term_id=prior_term.id,
        renewal_term_id=renewal_term.id,
    )
    session.add(comparison)
    session.flush()
    for difference in diff_terms(session, prior_term, renewal_term):
        materiality, rule_id = classify(difference, rules)
        session.add(
            Difference(
                comparison_id=comparison.id,
                field_path=difference.field_path,
                prior_value=difference.prior_value,
                renewal_value=difference.renewal_value,
                materiality=materiality,
                rule_id=rule_id,
            )
        )
    session.flush()
    session.refresh(comparison)
    return comparison


def breakdown_for(session: Session, comparison: Comparison) -> PremiumBreakdown:
    prior = session.get(PolicyTerm, comparison.prior_term_id)
    renewal = session.get(PolicyTerm, comparison.renewal_term_id)
    return attribute_premium(
        term_field_map(session, prior), term_field_map(session, renewal)
    )


def reclassify(
    session: Session,
    difference: Difference,
    to_materiality: str,
    note: str | None = None,
) -> Reclassification:
    log = Reclassification(
        difference_id=difference.id,
        from_materiality=difference.materiality,
        to_materiality=to_materiality,
        rule_id=difference.rule_id,
        note=note,
    )
    session.add(log)
    session.flush()
    return log
