"""The one search query.

Four ways in — page text, client name, policy number, carrier name — unioned to
one row per document. A document is findable whether or not it was classified
and whether or not it was matched to a client, because those paths involve
judgment and this one must not.

Ordered newest first rather than by rank: when she is on the phone she is
almost always after the most recent thing about someone, not the best textual
match.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import String, func, literal, select
from sqlalchemy.orm import Session

from renewal.models import (
    Carrier, Client, Document, DocumentClassification, DocumentLink,
    DocumentText, Policy,
)

NAME_SIMILARITY_FLOOR = 0.3


@dataclass(frozen=True)
class SearchResult:
    document_id: int
    client_id: int | None
    client_name: str | None
    doc_class: str | None
    uploaded_at: datetime
    carrier_name: str | None
    snippet: str
    matched_on: tuple[str, ...]


def _latest(model, fk: str, value_column: str):
    column = getattr(model, fk)
    ranked = select(
        column.label("parent_id"),
        getattr(model, value_column).label("value"),
        func.row_number()
        .over(partition_by=column, order_by=model.id.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.parent_id, ranked.c.value).where(
        ranked.c.rn == 1
    ).subquery()


def search(
    session: Session,
    q: str,
    *,
    client_id: int | None = None,
    doc_class: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SearchResult]:
    if not q.strip():
        return []

    tsquery = func.websearch_to_tsquery("english", q)
    links = _latest(DocumentLink, "document_id", "client_id")
    classes = _latest(DocumentClassification, "document_id", "doc_class")

    text_hits = select(DocumentText.document_id.label("document_id")).where(
        DocumentText.tsv.op("@@")(tsquery)
    )
    name_hits = (
        select(links.c.parent_id.label("document_id"))
        .join(Client, Client.id == links.c.value)
        .where(func.similarity(Client.display_name, q) > NAME_SIMILARITY_FLOOR)
    )
    policy_hits = (
        select(DocumentLink.document_id.label("document_id"))
        .join(Policy, Policy.id == DocumentLink.policy_id)
        .where(func.upper(Policy.policy_number) == q.strip().upper())
    )
    carrier_hits = (
        select(DocumentLink.document_id.label("document_id"))
        .join(Policy, Policy.id == DocumentLink.policy_id)
        .join(Carrier, func.lower(Carrier.display_name) == func.lower(
            Policy.carrier_name))
        .where(func.similarity(Carrier.display_name, q) > NAME_SIMILARITY_FLOOR)
    )

    matched = text_hits.union(name_hits, policy_hits, carrier_hits).subquery()

    matching_page = (
        select(DocumentText.text)
        .where(DocumentText.document_id == Document.id)
        .where(DocumentText.tsv.op("@@")(tsquery))
        .limit(1)
        .scalar_subquery()
    )
    snippet = func.ts_headline(
        "english",
        func.coalesce(matching_page, literal("", String)),
        tsquery,
        literal("StartSel=<mark>, StopSel=</mark>, MaxFragments=1, MaxWords=25"),
    )
    # Whether a page matched the text query is what separates "found in the
    # document" from "found by who it belongs to", and she should be able to
    # tell those apart in the results.
    matched_text = matching_page.is_not(None)

    query = (
        select(
            Document.id, Document.uploaded_at, links.c.value, Client.display_name,
            classes.c.value, Policy.carrier_name, snippet, matched_text,
        )
        .join(matched, matched.c.document_id == Document.id)
        .outerjoin(links, links.c.parent_id == Document.id)
        .outerjoin(Client, Client.id == links.c.value)
        .outerjoin(classes, classes.c.parent_id == Document.id)
        .outerjoin(DocumentLink, DocumentLink.document_id == Document.id)
        .outerjoin(Policy, Policy.id == DocumentLink.policy_id)
        .order_by(Document.uploaded_at.desc(), Document.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if client_id is not None:
        query = query.where(links.c.value == client_id)
    if doc_class is not None:
        query = query.where(classes.c.value == doc_class)
    if start is not None:
        query = query.where(Document.uploaded_at >= start)
    if end is not None:
        query = query.where(Document.uploaded_at <= end)

    seen: set[int] = set()
    out: list[SearchResult] = []
    for row in session.execute(query):
        document_id = row[0]
        if document_id in seen:
            continue
        seen.add(document_id)
        out.append(
            SearchResult(
                document_id=document_id,
                uploaded_at=row[1],
                client_id=row[2],
                client_name=row[3],
                doc_class=row[4],
                carrier_name=row[5],
                snippet=row[6] or "",
                matched_on=("text",) if row[7] else ("name",),
            )
        )
    return out
