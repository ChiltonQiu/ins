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


from pathlib import Path

from evals.accuracy import baseline_path


def test_baseline_path_starts_with_a_readable_slug():
    path = baseline_path(Path("evals/baselines"), "anthropic", "claude-opus-5", "v1")
    assert path.name.startswith("anthropic__claude-opus-5__v1-")
    assert path.suffix == ".json"


def test_characters_that_are_illegal_in_a_filename_are_replaced():
    """Ollama model names carry a colon; HuggingFace repo ids carry a slash."""
    assert baseline_path(Path("b"), "ollama", "qwen2.5:7b", "v1").name.startswith(
        "ollama__qwen2.5-7b__v1-"
    )
    assert baseline_path(
        Path("b"), "huggingface", "meta-llama/Llama-3.1-8B", "v1"
    ).name.startswith("huggingface__meta-llama-Llama-3.1-8B__v1-")


def test_the_path_is_stable_across_calls():
    args = (Path("b"), "ollama", "qwen2.5:7b", "v1")
    assert baseline_path(*args) == baseline_path(*args)


def test_models_differing_only_by_an_illegal_character_do_not_collide():
    """The readable slug alone maps both of these to "ollama__qwen2.5-7b__v1".
    A collision here would silently gate one model's run against another
    model's recorded baseline."""
    assert (
        baseline_path(Path("b"), "ollama", "qwen2.5:7b", "v1")
        != baseline_path(Path("b"), "ollama", "qwen2.5-7b", "v1")
    )


def test_the_separator_cannot_be_forged_out_of_a_provider_or_model_name():
    """"_" survives sanitisation, so slug text alone is ambiguous about where
    the provider ends and the model begins."""
    assert (
        baseline_path(Path("b"), "a_", "b", "v1")
        != baseline_path(Path("b"), "a", "_b", "v1")
    )


def test_two_different_models_never_share_a_baseline_file():
    a = baseline_path(Path("b"), "ollama", "qwen2.5:7b", "v1")
    b = baseline_path(Path("b"), "anthropic", "claude-opus-5", "v1")
    assert a != b
