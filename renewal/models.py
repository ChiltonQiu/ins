"""Every table here is insert-only. Application code never issues UPDATE or
DELETE: corrections, re-extractions, re-promotions, and draft edits all insert
new rows. Values extracted from documents are stored as text exactly as read;
typed parsing happens in the diff layer.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Client(Base):
    __tablename__ = "client"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    policies: Mapped[list["Policy"]] = relationship(back_populates="client")


class Policy(Base):
    """Identity, chosen by a human at upload. carrier_name and policy_number
    here are the label for the whole chain; the per-term values extracted from
    each document live on PolicyTerm."""

    __tablename__ = "policy"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    carrier_name: Mapped[str] = mapped_column(Text)
    policy_number: Mapped[str] = mapped_column(Text)
    line_of_business: Mapped[str] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    client: Mapped[Client] = relationship(back_populates="policies")
    terms: Mapped[list["PolicyTerm"]] = relationship(back_populates="policy")


class PolicyTerm(Base):
    """A frozen snapshot promoted from one extraction plus the corrections
    standing at that moment. Never updated: a later correction promotes a new
    row."""

    __tablename__ = "policy_term"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    carrier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_premium: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.id"), nullable=True
    )
    promoted_from_extraction_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction.id"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    policy: Mapped[Policy] = relationship(back_populates="terms")
    coverages: Mapped[list["Coverage"]] = relationship(back_populates="term")
    items: Mapped[list["InsuredItem"]] = relationship(back_populates="term")


class Coverage(Base):
    """insured_item_id NULL means policy-level (BI/PD, UM/UIM). Set means the
    coverage belongs to that vehicle (comp, collision), each with its own
    deductible and premium."""

    __tablename__ = "coverage"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    insured_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("insured_item.id"), nullable=True
    )
    coverage_code: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    limit_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    limit_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    deductible_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    premium: Mapped[str | None] = mapped_column(Text, nullable=True)
    term: Mapped[PolicyTerm] = relationship(back_populates="coverages")


class InsuredItem(Base):
    __tablename__ = "insured_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    item_type: Mapped[str] = mapped_column(Text)
    descriptor: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    term: Mapped[PolicyTerm] = relationship(back_populates="items")


class Document(Base):
    __tablename__ = "document"
    id: Mapped[int] = mapped_column(primary_key=True)
    blob_sha256: Mapped[str] = mapped_column(Text, index=True)
    original_filename: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer)
    has_text_layer: Mapped[bool] = mapped_column(Boolean)
    doc_type: Mapped[str] = mapped_column(Text)  # 'dec_page' in v0
    source: Mapped[str] = mapped_column(Text, server_default="manual_upload")
    agency_id: Mapped[int | None] = mapped_column(
        ForeignKey("agency.id"), nullable=True
    )
    uploaded_at: Mapped[datetime] = _created_at()


class Extraction(Base):
    __tablename__ = "extraction"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'partial', 'invalid_response', 'failed')",
            name="ck_extraction_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    extractor_version: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    document: Mapped[Document] = relationship()
    fields: Mapped[list["ExtractedField"]] = relationship(back_populates="extraction")


class ExtractedField(Base):
    __tablename__ = "extracted_field"
    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(ForeignKey("extraction.id"))
    field_path: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_text_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction: Mapped[Extraction] = relationship(back_populates="fields")


class Correction(Base):
    """The long-term asset. extracted_field_id is NULL for omissions (the model
    never emitted the field); corrected_value is NULL for hallucinations (the
    value is not on the document)."""

    __tablename__ = "correction"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('wrong_value', 'omission', 'hallucination')",
            name="ck_correction_kind",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(ForeignKey("extraction.id"))
    extracted_field_id: Mapped[int | None] = mapped_column(
        ForeignKey("extracted_field.id"), nullable=True
    )
    field_path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    extracted_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_at: Mapped[datetime] = _created_at()
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RenewalRun(Base):
    """Holds the document pair between upload and promotion, and becomes the
    audit trail of every promotion attempt for that pair."""

    __tablename__ = "renewal_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    prior_document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    renewal_document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    created_at: Mapped[datetime] = _created_at()


class Comparison(Base):
    __tablename__ = "comparison"
    id: Mapped[int] = mapped_column(primary_key=True)
    renewal_run_id: Mapped[int] = mapped_column(ForeignKey("renewal_run.id"))
    prior_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    renewal_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    created_at: Mapped[datetime] = _created_at()
    differences: Mapped[list["Difference"]] = relationship(back_populates="comparison")


class Difference(Base):
    __tablename__ = "difference"
    __table_args__ = (
        CheckConstraint(
            "materiality IN ('material', 'informational', 'noise')",
            name="ck_difference_materiality",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("comparison.id"))
    field_path: Mapped[str] = mapped_column(Text)
    prior_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    renewal_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    materiality: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    comparison: Mapped[Comparison] = relationship(back_populates="differences")


class Reclassification(Base):
    __tablename__ = "reclassification"
    id: Mapped[int] = mapped_column(primary_key=True)
    difference_id: Mapped[int] = mapped_column(ForeignKey("difference.id"))
    from_materiality: Mapped[str] = mapped_column(Text)
    to_materiality: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    reclassified_at: Mapped[datetime] = _created_at()
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Draft(Base):
    """The generated row has final_text and edited_at NULL. An edit inserts a
    new row carrying the same generated_text plus final_text. Latest wins."""

    __tablename__ = "draft"
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("comparison.id"))
    generated_text: Mapped[str] = mapped_column(Text)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Agency(Base):
    """One row. There is no auth and no tenancy; this exists so agency_id has
    a target and so the ics token and intake address have a home she can
    rotate from the UI."""

    __tablename__ = "agency"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    ics_token: Mapped[str] = mapped_column(Text, unique=True)
    intake_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Carrier(Base):
    __tablename__ = "carrier"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()


class CarrierAlias(Base):
    """carrier_name is free text on Policy and PolicyTerm, so name variants
    must resolve to one carrier. Exact normalized match only — a fuzzy carrier
    match that silently picked the wrong company would be invisible."""

    __tablename__ = "carrier_alias"
    id: Mapped[int] = mapped_column(primary_key=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey("carrier.id"))
    alias: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()


class CarrierAdmittedStatus(Base):
    """Per state: the same carrier can be admitted in one and surplus-lines in
    another. Human-set only, never inferred from a document. Append-only;
    the latest row per (carrier_id, state) wins."""

    __tablename__ = "carrier_admitted_status"
    __table_args__ = (
        CheckConstraint(
            "status IN ('admitted', 'non_admitted', 'unknown')",
            name="ck_carrier_admitted_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    carrier_id: Mapped[int] = mapped_column(ForeignKey("carrier.id"))
    state: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    set_by: Mapped[str] = mapped_column(Text, server_default="human")
    set_at: Mapped[datetime] = _created_at()


class PolicyBillingType(Base):
    """Her authoritative value, set by hand. Append-only; latest per policy_id
    wins. PolicyTerm.billing_type holds what a dec page said, and the overview
    flags a disagreement — because a disagreement means billing changed at
    renewal, which is itself worth seeing."""

    __tablename__ = "policy_billing_type"
    __table_args__ = (
        CheckConstraint(
            "billing_type IN ('direct_bill', 'agency_bill', 'unknown')",
            name="ck_policy_billing_type",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    billing_type: Mapped[str] = mapped_column(Text)
    set_by: Mapped[str] = mapped_column(Text, server_default="human")
    set_at: Mapped[datetime] = _created_at()


class DocumentText(Base):
    """Every document, always. This path has no judgment in it: search and date
    extraction read from here and never depend on field-extraction accuracy."""

    __tablename__ = "document_text"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", "extractor_version",
                         name="uq_document_text_page_version"),
        Index("ix_document_text_tsv", "tsv", postgresql_using="gin"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    page_number: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(Text)
    extractor_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
    )


class DocumentClassification(Base):
    """A coarse, low-stakes label that drives routing and display only. It never
    gates storage, search, or date extraction. Latest row wins.

    doc_class is deliberately unconstrained at the database level: a check
    constraint would turn a future label into a migration, and the value is
    display-only. The allowed set lives in renewal/classify/prompt_v1.py.
    """

    __tablename__ = "document_classification"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    doc_class: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    classifier_version: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class DocumentLink(Base):
    """Append-only; the latest row per document_id wins. A document with no row
    here is unmatched and appears in the queue — there is no status column to
    disagree with reality.

    candidates records the ranked list that was shown at the time. A manual row
    replacing an auto row is training data: these were offered, this was right.
    """

    __tablename__ = "document_link"
    __table_args__ = (
        CheckConstraint("method IN ('auto', 'manual')", name="ck_document_link_method"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"))
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy.id"), nullable=True
    )
    method: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    candidates: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = _created_at()


DATE_TYPES = (
    "policy_effective", "policy_expiration", "renewal_due",
    "cancellation_effective", "non_renewal_effective", "payment_due",
    "inspection_deadline", "remediation_deadline", "audit_date", "other",
)
_DATE_TYPE_SQL = ", ".join(f"'{t}'" for t in DATE_TYPES)


class DocumentDate(Base):
    """An immutable extracted fact. Human judgment lands in DateEvent.

    No client_id: it is derived through the document's latest DocumentLink, so
    re-assigning a misfiled document moves every date on it with no backfill
    and no stale rows.

    is_derived marks a value the system calculated rather than read. That is
    the one place in this design where a rendered date was never printed on the
    document, and it can never be auto-confirmed.
    """

    __tablename__ = "document_date"
    __table_args__ = (
        CheckConstraint(f"date_type IN ({_DATE_TYPE_SQL})",
                        name="ck_document_date_type"),
        # The attribute is pass_name because `pass` is a Python keyword; the
        # column keeps the spec's name, so the constraint quotes it.
        CheckConstraint("\"pass\" IN ('regex', 'llm')",
                        name="ck_document_date_pass"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    date_value: Mapped[date] = mapped_column(Date, index=True)
    date_type: Mapped[str] = mapped_column(Text)
    source_page: Mapped[int] = mapped_column(Integer)
    source_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    extractor_version: Mapped[str] = mapped_column(Text)
    pass_name: Mapped[str] = mapped_column("pass", Text)
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    anchor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    anchor_source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class DateEvent(Base):
    """'superseded' is written when a re-extraction at a newer version replaces
    a row she had already acted on, so her judgment is preserved rather than
    silently attached to a stale row."""

    __tablename__ = "date_event"
    __table_args__ = (
        CheckConstraint("action IN ('confirmed', 'dismissed', 'superseded')",
                        name="ck_date_event_action"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_date_id: Mapped[int] = mapped_column(
        ForeignKey("document_date.id"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, server_default="human")
    created_at: Mapped[datetime] = _created_at()


class ManualDate(Base):
    __tablename__ = "manual_date"
    __table_args__ = (
        CheckConstraint(f"date_type IN ({_DATE_TYPE_SQL})",
                        name="ck_manual_date_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agency.id"))
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("client.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text)
    date_value: Mapped[date] = mapped_column(Date, index=True)
    date_type: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, server_default="human")
    created_at: Mapped[datetime] = _created_at()


class ManualDateEvent(Base):
    __tablename__ = "manual_date_event"
    __table_args__ = (
        CheckConstraint("action IN ('confirmed', 'dismissed', 'superseded')",
                        name="ck_manual_date_event_action"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    manual_date_id: Mapped[int] = mapped_column(
        ForeignKey("manual_date.id"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, server_default="human")
    created_at: Mapped[datetime] = _created_at()
