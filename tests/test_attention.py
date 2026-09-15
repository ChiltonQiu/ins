import json
from datetime import date, timedelta

from renewal.attention.rules import (
    UNCONFIRMED_DATE_WINDOW_DAYS, evaluate, open_items, resolve,
)
from renewal.models import (
    AttentionItem, Client, Document, DocumentClassification, DocumentDate,
    DocumentLink,
)
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings

TODAY = date(2026, 6, 1)

# Several short lines, so the page clears MIN_CHARS_FOR_TEXT_LAYER and keeps
# its text layer instead of being routed to OCR.
def _lines(first):
    return [first,
            "Issued by the carrier for the policy named below.",
            "See the enclosed pages for the full terms of this notice."]


def _reasons(session, document_id):
    """Ingest already evaluated, so the invariant is the stored state rather
    than what a second evaluate() call returns."""
    return {row.reason_code for row in session.query(AttentionItem).filter_by(
        document_id=document_id)}


def _item(session, document_id, reason_code):
    return session.query(AttentionItem).filter_by(
        document_id=document_id, reason_code=reason_code).one()


def _linked_document(session, doc_class=None):
    """A document built directly, so evaluate() can be exercised on state that
    already exists. Going through the pipeline would evaluate it first, and
    these two tests are about what the rule does, not about ingest order."""
    document = Document(blob_sha256=f"{len(doc_class or ''):064x}",
                        original_filename="d.pdf", page_count=1,
                        has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    client = Client(display_name="Acme Landscaping LLC")
    session.add(client)
    session.flush()
    session.add(DocumentLink(document_id=document.id, client_id=client.id,
                             method="manual", confidence=1.0, candidates=[]))
    if doc_class:
        session.add(DocumentClassification(
            document_id=document.id, doc_class=doc_class, confidence=0.9,
            classifier_version="classify-v1", model_id="stub"))
    session.flush()
    return document


def _document(session, store, lines, doc_class="unknown"):
    from renewal.pipeline import ingest_document
    return ingest_document(
        session, store, data=make_text_pdf([lines]), original_filename="d.pdf",
        source="bulk_import", agency_id=1,
        model_client=StubClient(json.dumps({"doc_class": doc_class,
                                            "confidence": 0.9})),
        settings=_settings(),
    )


def test_a_cancellation_notice_is_flagged(session, store):
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    evaluate(session, document, settings=_settings())
    assert "cancellation_notice" in _reasons(session, document.id)


def test_a_non_renewal_notice_is_flagged(session, store):
    document = _document(session, store, _lines("NOTICE OF NON-RENEWAL"),
                         doc_class="non_renewal_notice")
    evaluate(session, document, settings=_settings())
    assert "non_renewal_notice" in _reasons(session, document.id)


def test_an_unmatched_document_is_flagged(session, store):
    document = _document(session, store, _lines("a page nobody can place"))
    evaluate(session, document, settings=_settings())
    assert "unmatched_document" in _reasons(session, document.id)


def test_a_matched_document_is_not_flagged_as_unmatched(session):
    document = _linked_document(session)
    evaluate(session, document, settings=_settings())
    assert "unmatched_document" not in _reasons(session, document.id)


def test_an_ordinary_invoice_is_not_flagged(session):
    """Over-flagging is the bias, not flagging everything."""
    document = _linked_document(session, doc_class="invoice")
    evaluate(session, document, settings=_settings())
    assert _reasons(session, document.id) == set()


def test_evaluation_is_idempotent(session, store):
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    evaluate(session, document, settings=_settings())
    evaluate(session, document, settings=_settings())
    assert session.query(AttentionItem).filter_by(
        document_id=document.id, reason_code="cancellation_notice").count() == 1


def test_a_near_unconfirmed_date_appears_in_the_queue_without_a_row(session, store):
    """It changes with the clock, so it is computed at read time rather than
    materialised — which would need a daemon we are not building."""
    document = _document(session, store, _lines("a page"))
    session.add(DocumentDate(
        document_id=document.id, date_value=TODAY + timedelta(days=7),
        date_type="cancellation_effective", source_page=1, source_text="x",
        confidence=0.5, extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    reasons = {r.reason_code for r in open_items(session, today=TODAY)}
    assert "unconfirmed_date_soon" in reasons
    assert session.query(AttentionItem).filter_by(
        reason_code="unconfirmed_date_soon").count() == 0


def test_a_date_beyond_the_window_is_not_in_the_queue(session, store):
    document = _document(session, store, _lines("a page"))
    session.add(DocumentDate(
        document_id=document.id,
        date_value=TODAY + timedelta(days=UNCONFIRMED_DATE_WINDOW_DAYS + 1),
        date_type="policy_expiration", source_page=1, source_text="x",
        confidence=0.5, extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()
    assert "unconfirmed_date_soon" not in {
        r.reason_code for r in open_items(session, today=TODAY)}


def test_a_confirmed_date_leaves_the_queue(session, store):
    from renewal.dates.service import confirm
    document = _document(session, store, _lines("a page"))
    row = DocumentDate(document_id=document.id,
                       date_value=TODAY + timedelta(days=7),
                       date_type="policy_expiration", source_page=1,
                       source_text="x", confidence=0.5,
                       extractor_version="dates-regex-v1", pass_name="regex")
    session.add(row)
    session.flush()
    confirm(session, row.id)
    assert "unconfirmed_date_soon" not in {
        r.reason_code for r in open_items(session, today=TODAY)}


def test_resolving_appends_an_event_and_clears_the_item(session, store):
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    item = _item(session, document.id, "cancellation_notice")
    resolve(session, item.id, action="done")
    assert item.id not in {r.item_id for r in open_items(session, today=TODAY)}


def test_nothing_resolves_itself(session, store):
    """We cannot detect that she replied without reading sent mail, which needs
    OAuth this project does not do."""
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    item = _item(session, document.id, "cancellation_notice")
    evaluate(session, document, settings=_settings())
    assert item.id in {r.item_id for r in open_items(session, today=TODAY)}


def test_a_resolved_item_is_never_revived_by_re_evaluation(session, store):
    """Her judgment outranks the rule that created the item."""
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    item = _item(session, document.id, "cancellation_notice")
    resolve(session, item.id, action="dismissed")
    evaluate(session, document, settings=_settings())
    assert item.id not in {r.item_id for r in open_items(session, today=TODAY)}


# renewal_received and premium_change. Both were declared in Phase 2 so the
# reason vocabulary would be stable across exactly this change.


def _policy_and_document(session, sha="f"):
    from renewal.models import Policy

    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(client_id=client.id, carrier_name="Progressive",
                    policy_number="PA-1", line_of_business="commercial_auto",
                    state="OR")
    session.add(policy)
    document = Document(blob_sha256=sha * 64, original_filename="d.pdf",
                        page_count=1, has_text_layer=True, doc_type="dec_page",
                        source="bulk_import", agency_id=1)
    session.add(document)
    session.flush()
    return policy, document


def _bound(session, policy, *, effective, premium="3900.00", kind="bound",
           document=None):
    from renewal.models import PolicyTerm

    term = PolicyTerm(policy_id=policy.id, kind=kind, carrier_name="Progressive",
                      effective_date=effective, total_premium=premium,
                      source_document_id=document.id if document else None)
    session.add(term)
    session.flush()
    return term


def test_a_renewal_term_on_an_existing_policy_is_flagged(session):
    """A fact about two rows, not a judgment about a PDF: a bound term on a
    policy that already holds a bound term with an earlier effective date."""
    from renewal.attention.rules import evaluate_promotion

    policy, document = _policy_and_document(session)
    _bound(session, policy, effective=date(2025, 7, 1))
    renewal = _bound(session, policy, effective=date(2026, 7, 1),
                     document=document)

    assert evaluate_promotion(session, renewal) is not None
    assert "renewal_received" in _reasons(session, document.id)


def test_the_first_term_on_a_policy_is_not_a_renewal(session):
    from renewal.attention.rules import evaluate_promotion

    policy, document = _policy_and_document(session)
    first = _bound(session, policy, effective=date(2025, 7, 1), document=document)
    assert evaluate_promotion(session, first) is None


def test_a_quoted_term_is_not_a_renewal(session):
    from renewal.attention.rules import evaluate_promotion

    policy, document = _policy_and_document(session)
    _bound(session, policy, effective=date(2025, 7, 1))
    quote = _bound(session, policy, effective=date(2026, 7, 1), kind="quoted",
                   document=document)
    assert evaluate_promotion(session, quote) is None


def test_a_backdated_term_is_not_a_renewal(session):
    """A term whose effective date precedes the existing one is a correction
    to history, not a renewal."""
    from renewal.attention.rules import evaluate_promotion

    policy, document = _policy_and_document(session)
    _bound(session, policy, effective=date(2026, 7, 1))
    backdated = _bound(session, policy, effective=date(2025, 7, 1),
                       document=document)
    assert evaluate_promotion(session, backdated) is None


def test_a_term_with_no_source_document_cannot_be_flagged(session):
    """Every attention item is a document plus a reason. A term promoted from
    a record import has no document to hang one on."""
    from renewal.attention.rules import evaluate_promotion

    policy, _ = _policy_and_document(session)
    _bound(session, policy, effective=date(2025, 7, 1))
    renewal = _bound(session, policy, effective=date(2026, 7, 1))
    assert evaluate_promotion(session, renewal) is None


def test_renewal_received_does_not_duplicate_on_a_second_run(session):
    from renewal.attention.rules import evaluate_promotion

    policy, document = _policy_and_document(session)
    _bound(session, policy, effective=date(2025, 7, 1))
    renewal = _bound(session, policy, effective=date(2026, 7, 1),
                     document=document)

    assert evaluate_promotion(session, renewal) is not None
    assert evaluate_promotion(session, renewal) is None
    assert session.query(AttentionItem).filter_by(
        document_id=document.id, reason_code="renewal_received").count() == 1


def test_premium_change_fires_above_the_threshold(session):
    from decimal import Decimal

    from renewal.attention.rules import evaluate_comparison

    _, document = _policy_and_document(session)
    item = evaluate_comparison(
        session, document_id=document.id, baseline_total=Decimal("3900.00"),
        total_delta=Decimal("500.00"), settings=_settings(),
    )
    assert item is not None
    assert "premium_change" in _reasons(session, document.id)
    assert "+500.00" in item.reason_text


def test_premium_change_is_quiet_below_the_threshold(session):
    from decimal import Decimal

    from renewal.attention.rules import evaluate_comparison

    _, document = _policy_and_document(session)
    assert evaluate_comparison(
        session, document_id=document.id, baseline_total=Decimal("3900.00"),
        total_delta=Decimal("39.00"), settings=_settings(),
    ) is None


def test_premium_change_reads_a_drop_as_well_as_a_rise(session):
    from decimal import Decimal

    from renewal.attention.rules import evaluate_comparison

    _, document = _policy_and_document(session)
    item = evaluate_comparison(
        session, document_id=document.id, baseline_total=Decimal("3900.00"),
        total_delta=Decimal("-500.00"), settings=_settings(),
    )
    assert item is not None
    assert "-500.00" in item.reason_text


def test_premium_change_needs_a_baseline_to_be_a_percentage_of(session):
    from decimal import Decimal

    from renewal.attention.rules import evaluate_comparison

    _, document = _policy_and_document(session)
    assert evaluate_comparison(
        session, document_id=document.id, baseline_total=None,
        total_delta=Decimal("500.00"), settings=_settings(),
    ) is None
    assert evaluate_comparison(
        session, document_id=document.id, baseline_total=Decimal("0.00"),
        total_delta=Decimal("500.00"), settings=_settings(),
    ) is None


def test_premium_change_does_not_duplicate_on_a_second_run(session):
    from decimal import Decimal

    from renewal.attention.rules import evaluate_comparison

    _, document = _policy_and_document(session)
    for _ in range(2):
        evaluate_comparison(
            session, document_id=document.id, baseline_total=Decimal("3900.00"),
            total_delta=Decimal("500.00"), settings=_settings(),
        )
    assert session.query(AttentionItem).filter_by(
        document_id=document.id, reason_code="premium_change").count() == 1


def test_premium_change_never_fires_on_a_quoted_column(session):
    """Cross-carrier, the delta is real but it is not a change to anything —
    it is two carriers pricing the same risk differently."""
    from renewal.comparison import ColumnSpec, build_matrix
    from renewal.materiality import load_rules

    policy, document = _policy_and_document(session)
    incumbent = _bound(session, policy, effective=date(2025, 7, 1),
                       premium="3900.00", document=document)
    quote = _bound(session, policy, effective=date(2026, 7, 1),
                   premium="4900.00", kind="quoted", document=document)
    build_matrix(
        session,
        columns=[ColumnSpec(incumbent.id, "baseline"),
                 ColumnSpec(quote.id, "comparand")],
        rules=load_rules("config/materiality.yaml"),
        settings=_settings(),
    )
    assert "premium_change" not in _reasons(session, document.id)


def test_building_a_renewal_comparison_flags_the_premium_move(session):
    from renewal.comparison import ColumnSpec, build_matrix
    from renewal.materiality import load_rules

    policy, document = _policy_and_document(session)
    prior = _bound(session, policy, effective=date(2025, 7, 1), premium="3900.00")
    renewal = _bound(session, policy, effective=date(2026, 7, 1),
                     premium="4400.00", document=document)
    build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"),
                 ColumnSpec(renewal.id, "comparand")],
        rules=load_rules("config/materiality.yaml"),
        settings=_settings(),
    )
    assert "premium_change" in _reasons(session, document.id)


def test_a_matrix_built_without_settings_flags_nothing(session):
    from renewal.comparison import ColumnSpec, build_matrix
    from renewal.materiality import load_rules

    policy, document = _policy_and_document(session)
    prior = _bound(session, policy, effective=date(2025, 7, 1), premium="3900.00")
    renewal = _bound(session, policy, effective=date(2026, 7, 1),
                     premium="4400.00", document=document)
    build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"),
                 ColumnSpec(renewal.id, "comparand")],
        rules=load_rules("config/materiality.yaml"),
    )
    assert "premium_change" not in _reasons(session, document.id)


def test_the_renewal_item_carries_a_link_to_the_picker(session):
    """The one click D10 describes: both terms preselected, nothing built
    until she takes it."""
    from renewal.attention.rules import evaluate_promotion

    policy, document = _policy_and_document(session)
    prior = _bound(session, policy, effective=date(2025, 7, 1))
    renewal = _bound(session, policy, effective=date(2026, 7, 1),
                     document=document)
    evaluate_promotion(session, renewal)

    row = next(r for r in open_items(session, today=TODAY)
               if r.reason_code == "renewal_received")
    assert row.compare_url == (
        f"/policies/{policy.id}/compare?baseline={prior.id}&comparand={renewal.id}"
    )


def test_other_items_carry_no_compare_link(session, store):
    document = _document(session, store, _lines("NOTICE OF CANCELLATION"),
                         doc_class="cancellation_notice")
    row = next(r for r in open_items(session, today=TODAY)
               if r.document_id == document.id)
    assert row.compare_url is None


def test_the_date_window_is_a_parameter(session, store):
    """How far ahead she wants warning is a judgment about how she works, so
    it is hers to set. The reason code stopped naming a number when the number
    stopped being fixed at fourteen."""
    document = _document(session, store, _lines("a page"))
    session.add(DocumentDate(
        document_id=document.id, date_value=TODAY + timedelta(days=25),
        date_type="policy_expiration", source_page=1, source_text="x",
        confidence=0.5, extractor_version="dates-regex-v1", pass_name="regex"))
    session.flush()

    def reasons(**kwargs):
        return {r.reason_code for r in open_items(session, today=TODAY, **kwargs)}

    assert "unconfirmed_date_soon" not in reasons()             # 14 days
    assert "unconfirmed_date_soon" in reasons(window_days=30)
