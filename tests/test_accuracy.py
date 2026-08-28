import json

from evals.accuracy import (
    Fixture,
    accuracy,
    load_fixtures,
    regressions,
    report,
    score,
)


def test_score_marks_exact_matches():
    expected = {"policy.total_premium": "1840.00", "coverage.BI.limit_value": "100/300"}
    actual = {"policy.total_premium": "1840.00", "coverage.BI.limit_value": "50/100"}
    assert score(expected, actual) == {
        "policy.total_premium": True,
        "coverage.BI.limit_value": False,
    }


def test_missing_field_scores_false_rather_than_vanishing():
    assert score({"policy.total_premium": "1840.00"}, {}) == {
        "policy.total_premium": False
    }


def test_extra_field_the_fixture_does_not_label_is_ignored():
    result = score({"policy.total_premium": "1"}, {"policy.total_premium": "1", "x": "y"})
    assert result == {"policy.total_premium": True}


def test_accuracy_is_the_fraction_correct():
    assert accuracy({"a": True, "b": False, "c": True}) == 2 / 3


def test_regressions_names_fields_that_passed_before_and_fail_now():
    baseline = {"f1": {"a": True, "b": False}}
    current = {"f1": {"a": False, "b": False}}
    assert regressions(baseline, current) == ["f1:a"]


def test_newly_passing_fields_are_not_regressions():
    assert regressions({"f1": {"a": False}}, {"f1": {"a": True}}) == []


def test_load_fixtures_reads_labelled_json(tmp_path):
    (tmp_path / "one.json").write_text(
        json.dumps(
            {
                "fixture_id": "progressive-auto-001",
                "carrier": "Progressive",
                "pdf_filename": "progressive-auto-001.pdf",
                "fields": {"policy.total_premium": "1840.00"},
            }
        )
    )
    fixtures = load_fixtures(tmp_path)
    assert fixtures == [
        Fixture(
            fixture_id="progressive-auto-001",
            carrier="Progressive",
            pdf_filename="progressive-auto-001.pdf",
            fields={"policy.total_premium": "1840.00"},
        )
    ]


def test_report_breaks_accuracy_down_by_carrier_and_field_path():
    text = report(
        {
            "f1": {"carrier": "Progressive", "fields": {"policy.total_premium": True}},
            "f2": {"carrier": "Progressive", "fields": {"policy.total_premium": False}},
        }
    )
    assert "Progressive" in text
    assert "policy.total_premium" in text
    assert "50" in text  # 1 of 2 correct
