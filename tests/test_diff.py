import pytest

from renewal.diff import RawDifference, diff_terms, normalize, term_field_map
from renewal.models import Client, Coverage, InsuredItem, Policy, PolicyTerm


@pytest.fixture
def policy(session):
    client = Client(display_name="Ramirez Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="personal_auto",
    )
    session.add(policy)
    session.flush()
    return policy


def _term(session, policy, *, premium, vehicles, policy_coverages=None, forms=None):
    term = PolicyTerm(
        policy_id=policy.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        effective_date="2026-03-01",
        total_premium=premium,
    )
    session.add(term)
    session.flush()
    for code, limit in (policy_coverages or {}).items():
        session.add(
            Coverage(policy_term_id=term.id, coverage_code=code, limit_value=limit)
        )
    for key, spec in vehicles.items():
        item = InsuredItem(
            policy_term_id=term.id,
            item_type="vehicle",
            descriptor=spec["descriptor"],
            attributes={"item_key": key},
        )
        session.add(item)
        session.flush()
        for code, leaves in spec.get("coverages", {}).items():
            session.add(
                Coverage(
                    policy_term_id=term.id,
                    insured_item_id=item.id,
                    coverage_code=code,
                    deductible_value=leaves.get("deductible_value"),
                    premium=leaves.get("premium"),
                )
            )
    for number, edition in (forms or {}).items():
        session.add(
            InsuredItem(
                policy_term_id=term.id,
                item_type="form",
                descriptor=number,
                attributes={"item_key": number, "edition_date": edition},
            )
        )
    session.flush()
    return term


def test_term_field_map_uses_the_canonical_grammar(session, policy):
    term = _term(
        session,
        policy,
        premium="1840.00",
        policy_coverages={"BI": "100/300"},
        vehicles={
            "VIN0001": {
                "descriptor": "2018 Ford F-150",
                "coverages": {"COLL": {"deductible_value": "500", "premium": "412.00"}},
            }
        },
        forms={"A085": "2019-06"},
    )
    field_map = term_field_map(session, term)
    assert field_map["policy.total_premium"] == "1840.00"
    assert field_map["coverage.BI.limit_value"] == "100/300"
    assert field_map["item.VIN0001.coverage.COLL.deductible_value"] == "500"
    assert field_map["forms.A085.edition_date"] == "2019-06"


def test_money_is_normalized_before_comparison():
    assert normalize("policy.total_premium", "$1,840.00") == normalize(
        "policy.total_premium", "1840.0"
    )


def test_whitespace_is_normalized_before_comparison():
    assert normalize("item.VIN1.descriptor", "2018  Ford   F-150") == normalize(
        "item.VIN1.descriptor", "2018 Ford F-150"
    )


def test_unchanged_fields_produce_no_difference(session, policy):
    kwargs = dict(premium="1840.00", vehicles={"VIN0001": {"descriptor": "F-150"}})
    prior = _term(session, policy, **kwargs)
    renewal = _term(session, policy, **kwargs)
    assert diff_terms(session, prior, renewal) == []


def test_changed_premium_produces_a_difference_with_raw_values(session, policy):
    prior = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    renewal = _term(
        session, policy, premium="2180.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    assert RawDifference(
        field_path="policy.total_premium",
        prior_value="1840.00",
        renewal_value="2180.00",
    ) in diff_terms(session, prior, renewal)


def test_added_vehicle_appears_as_a_one_sided_difference(session, policy):
    prior = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    renewal = _term(
        session,
        policy,
        premium="2180.00",
        vehicles={"V1": {"descriptor": "F-150"}, "V2": {"descriptor": "2019 Civic"}},
    )
    added = [
        d for d in diff_terms(session, prior, renewal)
        if d.field_path == "item.V2.descriptor"
    ]
    assert added == [
        RawDifference(
            field_path="item.V2.descriptor",
            prior_value=None,
            renewal_value="2019 Civic",
        )
    ]


def test_dropped_vehicle_appears_as_a_one_sided_difference(session, policy):
    prior = _term(
        session,
        policy,
        premium="1840.00",
        vehicles={"V1": {"descriptor": "F-150"}, "V2": {"descriptor": "2019 Civic"}},
    )
    renewal = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    dropped = [
        d for d in diff_terms(session, prior, renewal)
        if d.field_path == "item.V2.descriptor"
    ]
    assert dropped[0].renewal_value is None


def test_per_vehicle_deductibles_do_not_collide(session, policy):
    prior = _term(
        session,
        policy,
        premium="1840.00",
        vehicles={
            "V1": {"descriptor": "F-150", "coverages": {"COLL": {"deductible_value": "500"}}},
            "V2": {"descriptor": "Civic", "coverages": {"COLL": {"deductible_value": "500"}}},
        },
    )
    renewal = _term(
        session,
        policy,
        premium="1840.00",
        vehicles={
            "V1": {"descriptor": "F-150", "coverages": {"COLL": {"deductible_value": "1000"}}},
            "V2": {"descriptor": "Civic", "coverages": {"COLL": {"deductible_value": "500"}}},
        },
    )
    paths = [d.field_path for d in diff_terms(session, prior, renewal)]
    assert paths == ["item.V1.coverage.COLL.deductible_value"]


def test_sub_dollar_rounding_still_emits_a_difference(session, policy):
    """Noise is a label, not a filter — the row must exist to be classified."""
    prior = _term(
        session, policy, premium="1840.00", vehicles={"V1": {"descriptor": "F-150"}}
    )
    renewal = _term(
        session, policy, premium="1840.40", vehicles={"V1": {"descriptor": "F-150"}}
    )
    assert any(
        d.field_path == "policy.total_premium"
        for d in diff_terms(session, prior, renewal)
    )
