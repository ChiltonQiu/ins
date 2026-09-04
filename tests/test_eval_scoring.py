import json

from evals.accuracy import (
    DateScore,
    MatchScore,
    score_classification,
    load_fixtures,
    score_dates,
    score_match,
)


def _d(value, type_):
    return {"date_value": value, "date_type": type_}


def test_exact_match_is_a_true_positive():
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "policy_expiration")])
    assert got == DateScore(tp=1, fp=0, fn=0, precision=1.0, recall=1.0)


def test_right_date_wrong_type_is_both_a_miss_and_a_false_positive():
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "renewal_due")])
    assert (got.tp, got.fp, got.fn) == (0, 1, 1)


def test_over_extraction_costs_precision_not_recall():
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "policy_expiration"),
                       _d("2026-01-01", "other")])
    assert got.recall == 1.0
    assert got.precision == 0.5


def test_duplicate_extractions_of_one_expected_date_count_once():
    """Both passes store their own row; the harness must not double-count."""
    got = score_dates([_d("2026-07-01", "policy_expiration")],
                      [_d("2026-07-01", "policy_expiration"),
                       _d("2026-07-01", "policy_expiration")])
    assert (got.tp, got.fp, got.fn) == (1, 0, 0)


def test_payment_due_is_not_scored_against_a_direct_bill_policy():
    """Spec: for direct-bill policies these dates are genuinely not in the
    documents she receives, so absence is not a recall failure."""
    got = score_dates([_d("2026-07-01", "payment_due")], [], billing_type="direct_bill")
    assert (got.tp, got.fp, got.fn) == (0, 0, 0)
    assert got.recall == 1.0


def test_payment_due_is_scored_against_an_agency_bill_policy():
    got = score_dates([_d("2026-07-01", "payment_due")], [], billing_type="agency_bill")
    assert got.fn == 1


def test_empty_expectation_has_perfect_recall():
    assert score_dates([], []).recall == 1.0

def test_correct_label():
    assert score_classification("declarations", "declarations") == "correct"


def test_unknown_is_declined_not_wrong():
    assert score_classification("declarations", "unknown") == "declined"


def test_confident_wrong_guess_is_wrong():
    assert score_classification("declarations", "invoice") == "wrong"


def test_expected_unknown_answered_unknown_is_correct():
    assert score_classification("unknown", "unknown") == "correct"


def test_top1_match():
    assert score_match(7, [7, 3, 9]) == MatchScore(top1=True, in_top_k=True)


def test_right_answer_offered_but_not_first():
    assert score_match(7, [3, 9, 7]) == MatchScore(top1=False, in_top_k=True)


def test_right_answer_never_offered():
    assert score_match(7, [3, 9]) == MatchScore(top1=False, in_top_k=False)


def test_no_expected_client_and_nothing_offered_is_a_hit():
    """A document that genuinely matches no client is correctly unmatched."""
    assert score_match(None, []) == MatchScore(top1=True, in_top_k=True)


def test_no_expected_client_but_something_offered_is_a_miss():
    assert score_match(None, [3]) == MatchScore(top1=False, in_top_k=False)


def test_fixture_without_new_keys_still_loads(tmp_path):
    """Existing fixture files predate these keys and must not break."""
    (tmp_path / "a.json").write_text(json.dumps({
        "fixture_id": "a", "carrier": "Progressive",
        "pdf_filename": "a.pdf", "fields": {"policy.number": "X1"},
    }))
    fixture = load_fixtures(tmp_path)[0]
    assert fixture.dates == []
    assert fixture.doc_class == "unknown"
    assert fixture.expected_client is None
    assert fixture.billing_type == "unknown"


def test_fixture_with_new_keys_loads_them(tmp_path):
    (tmp_path / "b.json").write_text(json.dumps({
        "fixture_id": "b", "carrier": "Progressive",
        "pdf_filename": "b.pdf", "fields": {},
        "doc_class": "declarations",
        "expected_client": "Acme Landscaping LLC",
        "billing_type": "direct_bill",
        "dates": [{"date_value": "2026-07-01", "date_type": "policy_expiration"}],
    }))
    fixture = load_fixtures(tmp_path)[0]
    assert fixture.doc_class == "declarations"
    assert fixture.billing_type == "direct_bill"
    assert fixture.dates[0]["date_type"] == "policy_expiration"


def _date_entry(recall=0.5, precision=0.25, match=None):
    entry = {"carrier": "Progressive",
             "score": {"tp": 1, "fp": 3, "fn": 1,
                       "precision": precision, "recall": recall}}
    if match is not None:
        entry["match"] = match
    return entry


def test_date_report_leads_with_recall_per_fixture():
    from evals.accuracy import date_report

    out = date_report({"acme-dec": _date_entry()})
    assert "acme-dec" in out
    assert "recall  50.0%" in out
    assert "overall recall     50.0%" in out


def test_date_report_separates_top_1_from_recall_at_5():
    """A wrong auto-link and a merely bad candidate list are different
    failures, so one number must never hide the other."""
    from evals.accuracy import date_report

    out = date_report({
        "a": _date_entry(match={"top1": False, "in_top_k": True}),
        "b": _date_entry(match={"top1": True, "in_top_k": True}),
    })
    assert "client match top-1     50.0%" in out
    assert "client match recall@5 100.0%" in out


def test_date_report_omits_the_match_lines_when_nothing_was_scored():
    from evals.accuracy import date_report

    out = date_report({"a": _date_entry()})
    assert "client match" not in out


def test_date_report_of_an_empty_run_says_nothing_rather_than_dividing_by_zero():
    from evals.accuracy import date_report

    assert "overall" not in date_report({})


def test_classification_report_counts_declined_beside_correct():
    """Folding declined into wrong would train the harness against unknown,
    which is the one answer the spec asks for when the model is unsure."""
    from evals.accuracy import classification_report

    out = classification_report({
        "a": {"classification": "correct"},
        "b": {"classification": "declined"},
        "c": {"classification": "wrong"},
        "d": {"classification": "correct"},
    })
    assert "correct      2   50.0%" in out
    assert "declined     1   25.0%" in out
    assert "wrong        1   25.0%" in out


def test_classification_report_of_an_unscored_run_is_empty():
    from evals.accuracy import classification_report

    assert classification_report({"a": {"score": {}}}) == ""
