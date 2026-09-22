"""What happened, as numbers somebody outside the agency can safely read.

Two sources, and neither was built for this. Most of what matters was already
being recorded because the application records decisions: a correction says
what the model produced and what the truth was, a manual link says the match
was wrong, a dismissed date says the extractor found something that was not
there. Those rows are an evaluation set that writes itself. UsageEvent adds
the half the database could not know — which screens, how often, in what
order, how slow.

**Nothing here may name a client, a document or a premium.** The whole point
of this file is that its output leaves: it gets mailed, pasted into a chat,
handed to a model. So it emits counts, distributions, durations and field
paths — a field path is schema, not data — and never a value read off
somebody's insurance policy. Every query below is written to make that true by
construction rather than by remembering.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import Float, case, func, select
from sqlalchemy.orm import Session

from renewal.models import (
    AttentionEvent, AttentionItem, Comparison, Correction, DateEvent,
    Document, DocumentClassification, DocumentDate, DocumentLink, Extraction,
    ExtractedField, MailPollState, ManualDate, ManualDateEvent, PolicyTerm,
    Reclassification, UsageEvent, UserSession,
)

SCHEMA_VERSION = 1


def _counts(rows) -> dict[str, int]:
    return {str(k): int(v) for k, v in rows if k is not None}


def _spread(values: list[float]) -> dict | None:
    """Median and edges rather than a mean.

    One document that sat in the queue over a long weekend drags an average
    somewhere nobody's Tuesday actually was.
    """
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 2),
        "median": round(statistics.median(ordered), 2),
        "p90": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))], 2),
        "max": round(ordered[-1], 2),
    }


def _window(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# --------------------------------------------------------------- what arrived

def intake(session: Session, since: datetime) -> dict:
    by_source = _counts(session.execute(
        select(Document.source, func.count(Document.id))
        .where(Document.uploaded_at >= since).group_by(Document.source)
    ))
    day = func.to_char(Document.uploaded_at, 'YYYY-MM-DD')
    by_day = _counts(session.execute(
        select(day, func.count(Document.id))
        .where(Document.uploaded_at >= since).group_by(day).order_by(day)
    ))
    layer = case((Document.has_text_layer, 'text'), else_='scanned')
    polls = [
        {
            "host_configured": bool(row.host),
            "last_polled_at": row.last_polled_at.isoformat() if row.last_polled_at else None,
            "last_seen": row.last_seen,
            "last_ingested": row.last_ingested,
            # The message, not the mailbox: an error string can carry a host
            # and a folder name and nothing worse.
            "last_error": row.last_error,
        }
        for row in session.scalars(select(MailPollState))
    ]
    return {
        "documents_by_source": by_source,
        "documents_by_day": by_day,
        "total": sum(by_source.values()),
        "mail_poll_state": polls,
        "text_layer": _counts(session.execute(
            select(layer, func.count(Document.id))
            .where(Document.uploaded_at >= since).group_by(layer)
        )),
        "pages": _spread([
            float(p) for p in session.scalars(
                select(Document.page_count).where(Document.uploaded_at >= since)
            ) if p
        ]),
    }


# ------------------------------------------------------- what the machine did

def pipeline(session: Session, since: datetime) -> dict:
    classes = _counts(session.execute(
        select(DocumentClassification.doc_class, func.count())
        .join(Document, Document.id == DocumentClassification.document_id)
        .where(Document.uploaded_at >= since)
        .group_by(DocumentClassification.doc_class)
    ))
    extraction_status = _counts(session.execute(
        select(Extraction.status, func.count())
        .join(Document, Document.id == Extraction.document_id)
        .where(Document.uploaded_at >= since).group_by(Extraction.status)
    ))
    # The verification gate: a field whose quoted source text was not on the
    # page it cited. This is the single most diagnostic number in the file —
    # it is the extractor being caught, by the extractor's own rule.
    fields = session.execute(
        select(
            func.count(ExtractedField.id),
            func.sum(case((ExtractedField.needs_review, 1), else_=0)),
            func.sum(case((ExtractedField.validation_error.is_not(None), 1), else_=0)),
            func.avg(func.cast(ExtractedField.confidence, Float)),
        )
        .join(Extraction, Extraction.id == ExtractedField.extraction_id)
        .join(Document, Document.id == Extraction.document_id)
        .where(Document.uploaded_at >= since)
    ).one()
    total_fields, flagged, unverifiable, mean_conf = fields
    return {
        "document_status": _counts(session.execute(
            select(Document.status, func.count())
            .where(Document.uploaded_at >= since).group_by(Document.status)
        )),
        "classified_as": classes,
        "extractions": extraction_status,
        "fields": {
            "total": int(total_fields or 0),
            "needs_review": int(flagged or 0),
            "failed_verification": int(unverifiable or 0),
            "mean_confidence": round(float(mean_conf), 4) if mean_conf else None,
        },
        "promoted_terms": session.scalar(
            select(func.count(PolicyTerm.id)).where(PolicyTerm.created_at >= since)
        ),
        "comparisons_built": session.scalar(
            select(func.count(Comparison.id)).where(Comparison.created_at >= since)
        ),
        # How long a document takes from landing to finished, in minutes. The
        # number that answers "is she waiting on this".
        "minutes_to_settle": _spread([
            float(v) for v in session.scalars(
                select(
                    func.extract(
                        'epoch', Document.status_changed_at - Document.uploaded_at
                    ) / 60.0
                ).where(Document.uploaded_at >= since)
                 .where(Document.status != 'processing')
            ) if v is not None and v >= 0
        ]),
    }


# ----------------------------------------------------------- what she did

def corrections(session: Session, since: datetime) -> dict:
    """The asset. Every row is a labelled example of the extractor being wrong,
    and the field path says where — which is the difference between "it is
    inaccurate" and "it cannot read a deductible"."""
    by_kind = _counts(session.execute(
        select(Correction.kind, func.count())
        .where(Correction.corrected_at >= since).group_by(Correction.kind)
    ))
    by_path = _counts(session.execute(
        select(Correction.field_path, func.count())
        .where(Correction.corrected_at >= since)
        .group_by(Correction.field_path)
        .order_by(func.count().desc()).limit(40)
    ))
    return {
        "by_kind": by_kind,
        "by_field_path": by_path,
        "total": sum(by_kind.values()),
        # Which extractor was being corrected, so two versions can be
        # compared on the same corpus. It lives on the extraction, not the
        # correction, hence the join.
        "by_extractor_version": _counts(session.execute(
            select(Extraction.extractor_version, func.count())
            .join(Correction, Correction.extraction_id == Extraction.id)
            .where(Correction.corrected_at >= since)
            .group_by(Extraction.extractor_version)
        )),
    }


def judgements(session: Session, since: datetime) -> dict:
    """Confirm and dismiss, per thing being judged.

    A dismissal rate is a precision estimate nobody had to sit down and
    compute: she is telling the date extractor it was wrong, one keystroke at
    a time.
    """
    dates = _counts(session.execute(
        select(DateEvent.action, func.count())
        .where(DateEvent.created_at >= since).group_by(DateEvent.action)
    ))
    manual = _counts(session.execute(
        select(ManualDateEvent.action, func.count())
        .where(ManualDateEvent.created_at >= since).group_by(ManualDateEvent.action)
    ))
    links = _counts(session.execute(
        select(DocumentLink.method, func.count())
        .where(DocumentLink.created_at >= since).group_by(DocumentLink.method)
    ))
    attention_open = session.scalar(
        select(func.count(AttentionItem.id))
        .where(AttentionItem.created_at >= since)
    )
    attention_cleared = session.scalar(
        select(func.count(AttentionEvent.id))
        .where(AttentionEvent.created_at >= since)
    )
    return {
        "extracted_dates": dates,
        "typed_in_dates": manual,
        "dates_typed_by_hand": session.scalar(
            select(func.count(ManualDate.id)).where(ManualDate.created_at >= since)
        ),
        "document_links": links,
        "attention_raised": attention_open,
        "attention_cleared": attention_cleared,
        "materiality_overridden": session.scalar(
            select(func.count(Reclassification.id))
            .where(Reclassification.reclassified_at >= since)
        ),
        # Of the dates the extractor proposed, what share did she throw away.
        # None rather than zero when she has judged nothing: an untouched
        # queue is not a perfect extractor.
        "date_dismissal_rate": (
            round(dates.get("dismissed", 0) / sum(dates.values()), 3)
            if sum(dates.values()) else None
        ),
        "auto_link_rate": (
            round(links.get("auto", 0) / sum(links.values()), 3)
            if sum(links.values()) else None
        ),
    }


# ------------------------------------------------------------ how she used it

def usage(session: Session, since: datetime) -> dict:
    rows = list(session.scalars(
        select(UsageEvent).where(UsageEvent.occurred_at >= since)
        .order_by(UsageEvent.occurred_at)
    ))
    if not rows:
        return {"events": 0, "note": "No usage events. USAGE_TRACKING may be off."}

    views = [r for r in rows if r.method == "GET"]
    actions = [r for r in rows if r.method == "POST"]

    # A visit is a run of activity with no gap longer than half an hour. Not a
    # login session: she stays signed in for twelve hours and that would call
    # a whole day one visit.
    visits: list[list[UsageEvent]] = []
    for row in rows:
        if visits and (row.occurred_at - visits[-1][-1].occurred_at) <= timedelta(minutes=30):
            visits[-1].append(row)
        else:
            visits.append([row])

    hops = Counter(
        f"{r.from_route} -> {r.route}"
        for r in views if r.from_route and r.from_route != r.route
    )
    return {
        "events": len(rows),
        "page_views": len(views),
        "actions_taken": len(actions),
        "by_route": dict(Counter(r.route for r in views).most_common(40)),
        "actions_by_route": dict(Counter(r.route for r in actions).most_common(40)),
        "by_hour_local": dict(sorted(Counter(
            r.occurred_at.astimezone().hour for r in rows
        ).items())),
        "by_weekday": dict(sorted(Counter(
            r.occurred_at.astimezone().strftime("%a") for r in rows
        ).items())),
        "by_day": dict(sorted(Counter(
            r.occurred_at.astimezone().strftime("%Y-%m-%d") for r in rows
        ).items())),
        "most_common_paths": dict(hops.most_common(25)),
        "visits": {
            "count": len(visits),
            "minutes": _spread([
                (v[-1].occurred_at - v[0].occurred_at).total_seconds() / 60
                for v in visits
            ]),
            "events_each": _spread([float(len(v)) for v in visits]),
        },
        # Where the application is slow, which is a different complaint from
        # where it is confusing and gets mistaken for it constantly.
        "slowest_routes_ms": {
            route: _spread([float(r.duration_ms) for r in rows if r.route == route])
            for route, _ in Counter(r.route for r in rows).most_common(12)
        },
        "errors": dict(Counter(
            f"{r.status} {r.route}" for r in rows if r.status >= 400
        ).most_common(20)),
        "sign_ins": session.scalar(
            select(func.count(UserSession.id))
            .where(UserSession.created_at >= since)
        ),
    }


# --------------------------------------------------------------- where it hurt

def friction(session: Session, since: datetime) -> dict:
    """The parts that suggest something is wrong with the product rather than
    with a document."""
    retries = session.scalar(
        select(func.count(UsageEvent.id))
        .where(UsageEvent.occurred_at >= since)
        .where(UsageEvent.route == "/documents/{document_id}/retry")
    ) or 0
    reviews = Counter(
        r.route for r in session.scalars(
            select(UsageEvent).where(UsageEvent.occurred_at >= since)
            .where(UsageEvent.route == "/documents/{document_id}/review")
        )
    )
    # A document that has been waiting since before the window opened is the
    # one worth naming, and it is named by id because an id is not a client.
    waiting = session.execute(
        select(Document.id, Document.uploaded_at, Document.status)
        .where(Document.status != 'processed')
        .order_by(Document.uploaded_at)
        .limit(25)
    ).all()
    now = datetime.now(timezone.utc)
    return {
        "retries_pressed": retries,
        "review_screen_opened": reviews.get("/documents/{document_id}/review", 0),
        "unfinished_documents": [
            {
                "document_id": did,
                "status": status,
                "waiting_hours": round((now - up).total_seconds() / 3600, 1),
            }
            for did, up, status in waiting
        ],
        "documents_never_linked": session.scalar(
            select(func.count(Document.id))
            .where(Document.uploaded_at >= since)
            .where(~Document.id.in_(select(DocumentLink.document_id)))
        ),
        "dates_never_judged": session.scalar(
            select(func.count(DocumentDate.id))
            .where(DocumentDate.created_at >= since)
            .where(~DocumentDate.id.in_(select(DateEvent.document_date_id)))
        ),
    }


def report(session: Session, *, days: int = 30) -> dict:
    since = _window(days)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "window_start": since.isoformat(),
        "contains_no_client_data": True,
        "intake": intake(session, since),
        "pipeline": pipeline(session, since),
        "corrections": corrections(session, since),
        "judgements": judgements(session, since),
        "usage": usage(session, since),
        "friction": friction(session, since),
    }
