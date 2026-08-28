from pathlib import Path

import pytest

from renewal.diff import RawDifference
from renewal.materiality import classify, load_rules

RULES = Path("config/materiality.yaml")


@pytest.fixture(scope="module")
def rules():
    return load_rules(RULES)


def test_large_premium_change_is_material(rules):
    assert classify(
        RawDifference("policy.total_premium", "1840.00", "2180.00"), rules
    ) == ("material", "premium_total_change")


def test_premium_change_under_both_thresholds_is_noise(rules):
    materiality, rule_id = classify(
        RawDifference("policy.total_premium", "1840.00", "1840.40"), rules
    )
    assert materiality == "noise"
    assert rule_id == "premium_rounding"


def test_deductible_change_is_material_at_either_level(rules):
    assert classify(
        RawDifference("item.V1.coverage.COLL.deductible_value", "500", "1000"), rules
    )[0] == "material"
    assert classify(
        RawDifference("coverage.COMP.deductible_value", "500", "1000"), rules
    )[0] == "material"


def test_limit_change_is_material(rules):
    assert classify(
        RawDifference("coverage.BI.limit_value", "100/300", "50/100"), rules
    )[0] == "material"


def test_added_vehicle_is_material(rules):
    assert classify(
        RawDifference("item.V2.descriptor", None, "2019 Honda Civic"), rules
    )[0] == "material"


def test_carrier_change_is_material(rules):
    assert classify(
        RawDifference("policy.carrier_name", "Progressive", "Safeco"), rules
    )[0] == "material"


def test_garaging_address_change_is_informational(rules):
    assert classify(
        RawDifference("item.V1.attributes.garaging_zip", "78704", "78745"), rules
    )[0] == "informational"


def test_form_edition_change_is_noise(rules):
    assert classify(
        RawDifference("forms.A085.edition_date", "2019-06", "2024-01"), rules
    ) == ("noise", "form_edition")


def test_policy_number_reformatting_is_noise(rules):
    assert classify(
        RawDifference("policy.policy_number", "AU-4471", "AU4471"), rules
    ) == ("noise", "policy_number_format")


def test_unmatched_path_falls_to_the_default_rule(rules):
    assert classify(
        RawDifference("item.V1.attributes.odometer", "41000", "58000"), rules
    ) == ("informational", "default")


def test_first_matching_rule_wins(tmp_path):
    (tmp_path / "rules.yaml").write_text(
        "version: 1\n"
        "default: informational\n"
        "rules:\n"
        "  - id: first\n"
        "    match: {path_glob: '**.premium'}\n"
        "    materiality: material\n"
        "  - id: second\n"
        "    match: {path_glob: '**'}\n"
        "    materiality: noise\n"
    )
    rules = load_rules(tmp_path / "rules.yaml")
    assert classify(
        RawDifference("item.V1.coverage.COLL.premium", "412.00", "530.00"), rules
    ) == ("material", "first")
