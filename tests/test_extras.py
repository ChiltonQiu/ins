"""Carrier-specific fields, compared without promoting a column.

An extra is named `extras.<key>`, typed once in config/extras.yaml, and stored
in policy_term_extra. The diff, the rules and the screen never learn that a
path is an extra — it arrives in the flat field map like everything else.
"""

import pytest
import yaml

from renewal.comparison import ColumnSpec, build_matrix, matrix_for
from renewal.config import Settings
from renewal.corrections import record_correction
from renewal.diff import normalize, term_field_map, value_type_of
from renewal.extras import DEFAULT_TYPE, load_types
from renewal.fieldpath import is_valid
from renewal.materiality import load_rules
from renewal.models import (
    Client,
    Document,
    Extraction,
    Policy,
    PolicyTerm,
    PolicyTermExtra,
)
from renewal.promote import promote


def _write(tmp_path, types):
    path = tmp_path / "extras.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "default": "text", "types": types}))
    return path


# The grammar.


def test_one_segment_is_a_field_path():
    assert is_valid("extras.surcharge_total")


def test_a_nested_extra_is_not():
    """The type lives in config against the whole key, so a key with a dot in
    it would have no type to look up."""
    assert not is_valid("extras.a.b")
    assert not is_valid("extras.")
    assert not is_valid("extras")


# The types.


def test_an_unlisted_key_is_text(tmp_path):
    """text compares by exact string, which is the answer that cannot be wrong
    in an interesting way."""
    types = load_types(_write(tmp_path, {"extras.known": "money"}))
    assert types.get("extras.unknown", DEFAULT_TYPE) == "text"


def test_an_unknown_value_type_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown value type"):
        load_types(_write(tmp_path, {"extras.whatever": "currency"}))


def test_the_shipped_config_loads():
    assert load_types("config/extras.yaml") == {}


# Typed normalization.


def test_a_money_extra_compares_as_money():
    assert normalize("extras.surcharge", "$1,200.00", "money") == normalize(
        "extras.surcharge", "1200", "money"
    )


def test_a_text_extra_does_not():
    assert normalize("extras.note", "$1,200.00", "text") != normalize(
        "extras.note", "1200", "text"
    )


def test_a_core_path_still_guesses_from_its_leaf():
    """MONEY_LEAVES is right for a closed vocabulary and wrong for an open
    one, which is why extras pass the type and core paths do not."""
    assert normalize("coverage.COMP.premium", "$1,200.00") == normalize(
        "coverage.COMP.premium", "1200"
    )


def test_an_untyped_extra_is_not_read_as_money():
    """The open vocabulary is exactly where the leaf guess goes wrong:
    extras.premium would otherwise parse a policy number as a quantity.

    value_type_of is what keeps the guess away from extras — it answers text
    for a key nobody typed, so no caller ever reaches normalize's leaf guess
    with an extras path.
    """
    assert value_type_of("extras.premium", {}) == "text"
    value_type = value_type_of("extras.premium", {})
    assert normalize("extras.premium", "$1,200.00", value_type) != normalize(
        "extras.premium", "1200", value_type
    )


def test_a_core_path_has_no_type_and_keeps_guessing():
    assert value_type_of("coverage.COMP.premium", {"extras.x": "money"}) is None


# Promotion and read-back.


def _extraction(session, *, premium="3900.00"):
    document = Document(
        blob_sha256="e" * 64,
        original_filename="dec.pdf",
        page_count=1,
        has_text_layer=True,
        doc_type="dec_page",
    )
    session.add(document)
    session.flush()
    extraction = Extraction(
        document_id=document.id,
        extractor_version="v1",
        model_id="claude-opus-5",
        status="ok",
    )
    session.add(extraction)
    session.flush()
    from renewal.models import ExtractedField

    session.add(
        ExtractedField(
            extraction_id=extraction.id,
            field_path="policy.total_premium",
            value=premium,
            confidence=0.96,
            source_page=1,
            source_text_span=f"Total Policy Premium ${premium}",
        )
    )
    session.flush()
    return extraction


def _policy(session):
    client = Client(display_name="Acme Landscaping")
    session.add(client)
    session.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="PA-1",
        line_of_business="commercial_auto",
        state="OR",
    )
    session.add(policy)
    session.flush()
    return policy


def test_an_omission_correction_promotes_into_policy_term_extra(session):
    """The extractor is not touched. Extras arrive through add-missing-field
    on the review screen, which already writes an omission correction against
    any valid path."""
    policy = _policy(session)
    extraction = _extraction(session)
    record_correction(
        session,
        extraction_id=extraction.id,
        field_path="extras.terrorism_surcharge",
        kind="omission",
        corrected_value="$42.00",
    )
    session.flush()

    term = promote(session, extraction, policy.id)
    session.flush()
    extras = session.query(PolicyTermExtra).filter_by(policy_term_id=term.id).all()
    assert [(row.field_path, row.value) for row in extras] == [
        ("extras.terrorism_surcharge", "$42.00")
    ]


def test_an_extra_comes_back_out_of_the_field_map(session):
    policy = _policy(session)
    term = PolicyTerm(policy_id=policy.id, kind="bound", total_premium="3900.00")
    session.add(term)
    session.flush()
    session.add(
        PolicyTermExtra(
            policy_term_id=term.id,
            field_path="extras.terrorism_surcharge",
            value="42.00",
        )
    )
    session.flush()
    assert term_field_map(session, term)["extras.terrorism_surcharge"] == "42.00"


# End to end through the matrix.


def _settings(tmp_path, types):
    return Settings(
        database_url="unused",
        blob_root=tmp_path,
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config="config/materiality.yaml",
        extras_config=_write(tmp_path, types),
    )


def _pair(session, first, second):
    policy = _policy(session)
    terms = []
    for value in (first, second):
        term = PolicyTerm(policy_id=policy.id, kind="bound", total_premium="3900.00")
        session.add(term)
        session.flush()
        session.add(
            PolicyTermExtra(
                policy_term_id=term.id, field_path="extras.surcharge", value=value
            )
        )
        terms.append(term)
    session.flush()
    return terms


def test_a_money_extra_written_two_ways_is_not_a_difference(session, tmp_path):
    prior, renewal = _pair(session, "$1,200.00", "1200")
    settings = _settings(tmp_path, {"extras.surcharge": "money"})
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=load_rules("config/materiality.yaml"),
        settings=settings,
    )
    matrix = matrix_for(session, comparison, settings=settings)
    assert [row.difference.field_path for row in matrix.rows] == []


def test_the_same_pair_as_text_is_a_difference(session, tmp_path):
    prior, renewal = _pair(session, "$1,200.00", "1200")
    settings = _settings(tmp_path, {})
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=load_rules("config/materiality.yaml"),
        settings=settings,
    )
    matrix = matrix_for(session, comparison, settings=settings)
    assert [row.difference.field_path for row in matrix.rows] == ["extras.surcharge"]
    assert matrix.rows[0].comparands[0].differs


def test_no_settings_means_every_extra_is_text(session):
    """The safe direction: an unconfigured installation shows the difference
    rather than hiding it."""
    prior, renewal = _pair(session, "$1,200.00", "1200")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=load_rules("config/materiality.yaml"),
    )
    matrix = matrix_for(session, comparison)
    assert [row.difference.field_path for row in matrix.rows] == ["extras.surcharge"]


def test_a_rule_naming_extras_classifies_one(session, tmp_path):
    rules_path = tmp_path / "materiality.yaml"
    rules_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default": "informational",
                "rules": [
                    {
                        "id": "surcharge_appeared",
                        "match": {"path_glob": "extras.*"},
                        "materiality": "material",
                    }
                ],
            }
        )
    )
    prior, renewal = _pair(session, "0.00", "42.00")
    comparison = build_matrix(
        session,
        columns=[ColumnSpec(prior.id, "baseline"), ColumnSpec(renewal.id, "comparand")],
        rules=load_rules(rules_path),
    )
    matrix = matrix_for(session, comparison)
    assert matrix.rows[0].difference.rule_id == "surcharge_appeared"
    assert matrix.rows[0].difference.materiality == "material"
