from decimal import Decimal

from renewal.premium import attribute_premium


def test_no_total_premium_means_no_attribution():
    result = attribute_premium({}, {})
    assert result.available is False
    assert result.reason == "total premium is not present on both documents"
    assert result.lines == []


def test_matched_coverage_delta_is_attributed():
    prior = {
        "policy.total_premium": "1840.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    renewal = {
        "policy.total_premium": "1958.00",
        "item.V1.coverage.COLL.premium": "530.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.total_delta == Decimal("118.00")
    assert result.lines[0].field_path == "item.V1.coverage.COLL.premium"
    assert result.lines[0].amount == Decimal("118.00")
    assert result.residual == Decimal("0.00")


def test_added_item_contributes_its_whole_premium():
    prior = {"policy.total_premium": "1840.00"}
    renewal = {
        "policy.total_premium": "1994.00",
        "item.V2.coverage.COLL.premium": "154.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.lines[0].amount == Decimal("154.00")
    assert result.residual == Decimal("0.00")


def test_dropped_item_contributes_a_negative_amount():
    prior = {
        "policy.total_premium": "1994.00",
        "item.V2.coverage.COLL.premium": "154.00",
    }
    renewal = {"policy.total_premium": "1840.00"}
    result = attribute_premium(prior, renewal)
    assert result.lines[0].amount == Decimal("-154.00")
    assert result.residual == Decimal("0.00")


def test_unexplained_remainder_is_reported_as_residual():
    prior = {
        "policy.total_premium": "1840.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    renewal = {
        "policy.total_premium": "2180.00",
        "item.V1.coverage.COLL.premium": "530.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.total_delta == Decimal("340.00")
    assert sum(line.amount for line in result.lines) == Decimal("118.00")
    assert result.residual == Decimal("222.00")


def test_unchanged_line_premiums_are_not_listed():
    prior = {
        "policy.total_premium": "1840.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    renewal = {
        "policy.total_premium": "1900.00",
        "item.V1.coverage.COLL.premium": "412.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.lines == []
    assert result.residual == Decimal("60.00")


def test_unparseable_premium_is_treated_as_missing():
    result = attribute_premium(
        {"policy.total_premium": "see attached"},
        {"policy.total_premium": "2180.00"},
    )
    assert result.available is False


def test_labels_name_the_vehicle_and_coverage():
    prior = {"policy.total_premium": "1840.00"}
    renewal = {
        "policy.total_premium": "1994.00",
        "item.1FTEW1EP0JKD00001.coverage.COLL.premium": "154.00",
    }
    result = attribute_premium(prior, renewal)
    assert result.lines[0].label == "COLL on 1FTEW1EP0JKD00001"
