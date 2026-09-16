"""Every table here is insert-only. Application code never issues UPDATE or
DELETE: corrections, re-extractions, re-promotions, and draft edits all insert
new rows. Values extracted from documents are stored as text exactly as read;
typed parsing happens in the diff layer.

The two exceptions are `app_user` and `user_session`, at the bottom. Those
hold credentials rather than record: a session is deleted at logout, and a
lockout counter is updated in place. Keeping a history of session rows would
be a liability, not an audit trail.
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


def _decided_by() -> Mapped[int | None]:
    """Which account made this judgment.

    Nullable permanently: rows written before attribution existed have no user,
    and a row the pipeline wrote has none by definition. RESTRICT rather than
    CASCADE — a user who decided something cannot be deleted, because deleting
    them would take the record of the decision with them. is_active is how an
    account is turned off.
    """
    return mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


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
    row.

    kind='quoted' is a competitor's offer for the same risk, hanging off the
    incumbent's policy chain so that carrier_name means what it has always
    meant — what that term's document said. Every query that means "the
    current term" must filter kind='bound'; there is exactly one such query,
    _latest_term() in renewal/clients/overview.py.
    """

    __tablename__ = "policy_term"
    __table_args__ = (
        CheckConstraint("kind IN ('bound', 'quoted')", name="ck_policy_term_kind"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    kind: Mapped[str] = mapped_column(Text, server_default="bound")
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
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'processed', 'failed')",
            name="ck_document_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    blob_sha256: Mapped[str] = mapped_column(Text, index=True)
    original_filename: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer)
    has_text_layer: Mapped[bool] = mapped_column(Boolean)
    doc_type: Mapped[str] = mapped_column(Text)  # 'dec_page' in v0
    source: Mapped[str] = mapped_column(Text, server_default="manual_upload")
    # Only the in-flight fact. Every other thing the inbox shows is computed at
    # read time from rows the pipeline writes, because those change the moment
    # she acts and a stored copy would be stale immediately after the act that
    # fixed it.
    status: Mapped[str] = mapped_column(Text, server_default="processed")
    status_changed_at: Mapped[datetime] = _created_at()
    agency_id: Mapped[int | None] = mapped_column(
        ForeignKey("agency.id"), nullable=True
    )
    inbound_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_message.id"), nullable=True
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
    user_id: Mapped[int | None] = _decided_by()


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
    """One baseline column and up to four comparands, in comparison_column.

    prior_term_id and renewal_term_id are not written any more — the same term
    ids live on the columns. They stay because for every comparison built
    before the matrix they are the only record of what was compared, and
    matrix_for() reads them to render those rows. renewal_run_id is NULL for a
    comparison assembled from the record rather than from an upload pair.
    """

    __tablename__ = "comparison"
    id: Mapped[int] = mapped_column(primary_key=True)
    renewal_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("renewal_run.id"), nullable=True
    )
    prior_term_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_term.id"), nullable=True
    )
    renewal_term_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_term.id"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    user_id: Mapped[int | None] = _decided_by()
    differences: Mapped[list["Difference"]] = relationship(back_populates="comparison")


class Difference(Base):
    __tablename__ = "difference"
    __table_args__ = (
        CheckConstraint(
            "materiality IN ('material', 'informational', 'noise')",
            name="ck_difference_materiality",
        ),
        UniqueConstraint("comparison_id", "field_path", name="uq_difference_path"),
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
    user_id: Mapped[int | None] = _decided_by()


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


class ComparisonColumn(Base):
    """One column of a comparison. Exactly one row per comparison is the
    baseline and everything else is measured against it; the partial unique
    index in the migration is what enforces that, because a CHECK cannot see
    across rows.

    A comparison with no rows here was built before the matrix and is read
    through Comparison.prior_term_id and renewal_term_id instead. Absence is
    the marker: no status column, and nothing backfilled.
    """

    __tablename__ = "comparison_column"
    __table_args__ = (
        CheckConstraint(
            "role IN ('baseline', 'comparand')", name="ck_comparison_column_role"
        ),
        UniqueConstraint(
            "comparison_id", "position", name="uq_comparison_column_position"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_id: Mapped[int] = mapped_column(ForeignKey("comparison.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    policy_term_id: Mapped[int] = mapped_column(ForeignKey("policy_term.id"))
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class DifferenceCell(Base):
    """One column's value for one difference row.

    value NULL means the field is absent from that column — not on that
    document at all. That is a different thing from an empty string and it is
    usually why a cheaper quote is cheaper, so it renders as words rather than
    as a blank cell.
    """

    __tablename__ = "difference_cell"
    __table_args__ = (
        UniqueConstraint(
            "difference_id", "comparison_column_id", name="uq_difference_cell"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    difference_id: Mapped[int] = mapped_column(ForeignKey("difference.id"), index=True)
    comparison_column_id: Mapped[int] = mapped_column(
        ForeignKey("comparison_column.id")
    )
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class PolicyTermExtra(Base):
    """A carrier-specific field, kept out of the promoted columns.

    No value_type column: the type belongs to the key rather than to the term
    and lives in config/extras.yaml, so there is one copy of it instead of one
    per row.
    """

    __tablename__ = "policy_term_extra"
    __table_args__ = (
        UniqueConstraint("policy_term_id", "field_path", name="uq_policy_term_extra"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_term_id: Mapped[int] = mapped_column(
        ForeignKey("policy_term.id"), index=True
    )
    field_path: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Agency(Base):
    """One row. Accounts exist but tenancy does not: every account sees this
    one agency. It exists so agency_id has a target and so the ics token and
    intake address have a home she can rotate from the UI."""

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
    user_id: Mapped[int | None] = _decided_by()
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
    user_id: Mapped[int | None] = _decided_by()
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
    user_id: Mapped[int | None] = _decided_by()
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
    user_id: Mapped[int | None] = _decided_by()
    created_at: Mapped[datetime] = _created_at()


class InboundMessage(Base):
    """The message body is stored as a document too, not only its attachments:
    the carrier's explanation is frequently in the body while the attachment is
    a bare form, and deadlines are very often stated in prose in the body."""

    __tablename__ = "inbound_message"
    __table_args__ = (
        UniqueConstraint("agency_id", "message_id", name="uq_inbound_message_id"),
        CheckConstraint(
            "processing_status IN ('received', 'processed', 'quarantined',"
            " 'duplicate', 'failed')",
            name="ck_inbound_message_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable: quarantined and failed messages belong to no agency, and losing
    # them would make a misconfigured forwarding rule invisible. Postgres treats
    # NULLs as distinct in a unique constraint, so many such rows coexist.
    agency_id: Mapped[int | None] = mapped_column(
        ForeignKey("agency.id"), nullable=True
    )
    message_id: Mapped[str] = mapped_column(Text)
    from_address: Mapped[str] = mapped_column(Text)
    to_address: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_mime_blob_sha256: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class AgencySetting(Base):
    """An operator-editable setting, appended rather than updated.

    Latest row per key wins, which is PolicyBillingType's shape and for the
    same reason: what she had it set to last month explains an item that fired
    last month. A threshold that was 10% when an alert was raised and is 20%
    now is the difference between a bug and a decision.

    Values are text because that is what a form posts. Parsing and bounds live
    in renewal/settings_store.py beside the definition of each key, so an
    invalid value is refused at the form rather than stored and tripped over
    later.
    """

    __tablename__ = "agency_setting"
    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agency.id"), index=True)
    key: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class NotificationSend(Base):
    """One row per operator email sent.

    A row rather than a column on agency, because the count at the time is
    worth keeping: it is the only record of how big the backlog got, and the
    guard reads the timestamp off the newest row anyway.

    digest_date set means a daily summary, and it is what stops the next tick
    from sending a second one. NULL means the event-triggered email, of which
    a busy morning may hold several.
    """

    __tablename__ = "notification_send"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_count: Mapped[int] = mapped_column(Integer)
    digest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sent_at: Mapped[datetime] = _created_at()


# One summary per day, and no constraint at all on the event-triggered email.
# A CHECK cannot express "unique among the non-NULLs"; a partial unique index
# can. Declared here rather than in __table_args__ for the reason
# uq_app_user_email_lower is: it names a column the class body has not defined
# yet at the time the arguments are evaluated.
Index(
    "uq_notification_send_digest_date",
    NotificationSend.__table__.c.digest_date,
    unique=True,
    postgresql_where=NotificationSend.__table__.c.digest_date.isnot(None),
)



class MailPollState(Base):
    """How far the poller has read, per mailbox folder.

    An optimisation and nothing more. Correctness lives on
    InboundMessage.message_id, unique per agency: losing this row means the
    next poll re-reads the folder and dedupes, which is slow and right rather
    than fast and wrong.
    """

    __tablename__ = "mail_poll_state"
    __table_args__ = (
        UniqueConstraint("host", "folder", name="uq_mail_poll_state_folder"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(Text)
    # NULL until the first successful poll. A UIDVALIDITY that no longer
    # matches means the folder was rebuilt, and every UID remembered here is a
    # number about a folder that no longer exists.
    uid_validity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What the last attempt did, so a page can say so. An intake that has been
    # broken since Thursday must not be invisible.
    last_seen: Mapped[int] = mapped_column(Integer, server_default="0")
    last_ingested: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

class AttentionItem(Base):
    """Not a task manager: a list of documents that appear to need a human
    response, with a suggested reason. Never auto-resolves, never auto-acts."""

    __tablename__ = "attention_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    reason_code: Mapped[str] = mapped_column(Text)
    reason_text: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_at: Mapped[datetime] = _created_at()


class AttentionEvent(Base):
    __tablename__ = "attention_event"
    __table_args__ = (
        CheckConstraint("action IN ('done', 'dismissed')",
                        name="ck_attention_event_action"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    attention_item_id: Mapped[int] = mapped_column(
        ForeignKey("attention_item.id"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, server_default="human")
    user_id: Mapped[int | None] = _decided_by()
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    """A person who can sign in. Every account can do everything; there are no
    roles. `is_active` turns an account off without deleting the row, so a
    later change that attributes decisions to a user still has something to
    point at."""

    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class UserSession(Base):
    """Only the SHA-256 of the cookie value is stored, so a stolen database
    dump yields no usable session. Named UserSession rather than Session: the
    web modules all import SQLAlchemy's Session."""

    __tablename__ = "user_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_sha256: Mapped[str] = mapped_column(Text, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Uniqueness on lower(email) rather than on the column, so Anne@ and anne@
# cannot both exist. A functional index cannot be written inside
# __table_args__ without naming a column that does not exist until the class
# body has run, so it is declared here instead.
Index(
    "uq_app_user_email_lower",
    func.lower(User.__table__.c.email),
    unique=True,
)
