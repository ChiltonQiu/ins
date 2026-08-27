from renewal.fieldpath import glob_match, is_valid, item_key


def test_policy_paths_are_valid():
    assert is_valid("policy.total_premium")
    assert is_valid("policy.effective_date")


def test_coverage_paths_at_both_levels_are_valid():
    assert is_valid("coverage.BI.limit_value")
    assert is_valid("item.1FTEW1EP0JKD00001.coverage.COLL.deductible_value")


def test_item_paths_are_valid():
    assert is_valid("item.1FTEW1EP0JKD00001.descriptor")
    assert is_valid("item.2019-honda-civic.attributes.garaging_zip")


def test_unknown_paths_are_rejected():
    assert not is_valid("policy.agent_commission")
    assert not is_valid("coverage.BI")
    assert not is_valid("")


def test_item_key_prefers_vin():
    assert (
        item_key(vin="1FTEW1EP0JKD00001", year="2018", make="Ford", model="F-150")
        == "1FTEW1EP0JKD00001"
    )


def test_item_key_falls_back_to_normalized_year_make_model():
    assert item_key(vin=None, year="2019", make="Honda", model="Civic LX") == (
        "2019-honda-civic-lx"
    )


def test_single_star_matches_one_segment_only():
    assert glob_match("coverage.*.deductible_value", "coverage.COLL.deductible_value")
    assert not glob_match(
        "coverage.*.deductible_value",
        "item.VIN1.coverage.COLL.deductible_value",
    )


def test_double_star_matches_any_depth():
    assert glob_match("**.deductible_value", "coverage.COLL.deductible_value")
    assert glob_match(
        "**.deductible_value", "item.VIN1.coverage.COLL.deductible_value"
    )
