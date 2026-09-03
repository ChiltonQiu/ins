from evals.accuracy import DateScore, score_dates


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
