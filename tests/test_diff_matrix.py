"""Diffing N field sets against one baseline.

Pure: no session, no persistence, no rules. A path becomes a row when any
comparand disagrees with the baseline, and absence on one side is a
disagreement like any other.
"""

from renewal.diff import FieldSet, diff_field_sets


def _set(term_id, **values):
    return FieldSet(term_id=term_id, values=values)


def test_a_path_every_column_agrees_on_is_not_a_row():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "3900.00"}),
        [
            _set(2, **{"policy.total_premium": "3900.00"}),
            _set(3, **{"policy.total_premium": "3900.00"}),
        ],
    )
    assert rows == []


def test_one_disagreeing_comparand_makes_a_row_carrying_all_of_them():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "3900.00"}),
        [
            _set(2, **{"policy.total_premium": "3900.00"}),
            _set(3, **{"policy.total_premium": "4455.00"}),
        ],
    )
    assert len(rows) == 1
    assert rows[0].baseline_value == "3900.00"
    assert rows[0].comparand_values == ["3900.00", "4455.00"]


def test_comparand_values_stay_positional():
    """The list index is the column. A comparand that has nothing to say about
    a path holds None at its own position rather than being left out."""
    rows = diff_field_sets(
        _set(1, **{"coverage.BI.limit_value": "100/300"}),
        [_set(2), _set(3, **{"coverage.BI.limit_value": "50/100"})],
    )
    assert rows[0].comparand_values == [None, "50/100"]


def test_a_path_only_a_comparand_has_is_a_row():
    rows = diff_field_sets(
        _set(1),
        [_set(2, **{"extras.surcharge_total": "120.00"})],
    )
    assert rows[0].field_path == "extras.surcharge_total"
    assert rows[0].baseline_value is None


def test_normalisation_still_only_canonicalises_type():
    """$1,200 and 1200.00 are the same premium and not a row. This is the
    existing normalize(), reached through the new entry point."""
    rows = diff_field_sets(
        _set(1, **{"coverage.COMP.premium": "$1,200.00"}),
        [_set(2, **{"coverage.COMP.premium": "1200"})],
    )
    assert rows == []


def test_rows_come_back_in_path_order():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "1", "coverage.BI.limit_value": "1"}),
        [_set(2, **{"policy.total_premium": "2", "coverage.BI.limit_value": "2"})],
    )
    assert [r.field_path for r in rows] == [
        "coverage.BI.limit_value",
        "policy.total_premium",
    ]


def test_one_comparand_is_the_pairwise_case():
    rows = diff_field_sets(
        _set(1, **{"policy.total_premium": "3900.00"}),
        [_set(2, **{"policy.total_premium": "4210.00"})],
    )
    assert len(rows) == 1
    assert rows[0].comparand_values == ["4210.00"]


def test_no_comparands_is_no_rows():
    """Not a state the picker can produce, but the engine must not raise on
    it: an empty comparison is empty, not an error."""
    assert diff_field_sets(_set(1, **{"policy.total_premium": "1"}), []) == []
