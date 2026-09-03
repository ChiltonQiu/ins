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
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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
