from renewal.models import Client, Policy
from renewal.resolve.candidates import extract_candidates
from renewal.resolve.matching import rank_matches

DEC = """\
DECLARATIONS PAGE
Named Insured: Acme Landscaping LLC
Mailing Address: 1400 Harbor Blvd
Fullerton, CA 92832
Policy Number: CAP-7781-22
Effective Date: 07/01/2025
"""


def test_named_insured_is_read_from_its_label():
    assert extract_candidates(DEC).named_insured == "Acme Landscaping LLC"


def test_policy_number_is_read_from_its_label():
    assert "CAP-7781-22" in extract_candidates(DEC).policy_numbers


def test_address_lines_are_captured():
    lines = extract_candidates(DEC).address_lines
    assert any("Fullerton" in line for line in lines)


def test_missing_labels_yield_nothing_rather_than_a_guess():
    got = extract_candidates("A letter with no labels at all.")
    assert got.named_insured is None
    assert got.policy_numbers == []


def _seed(session):
    acme = Client(display_name="Acme Landscaping LLC")
    other = Client(display_name="Acme Landscaping Inc")
    session.add_all([acme, other])
    session.flush()
    policy = Policy(client_id=acme.id, carrier_name="Travelers",
                    policy_number="CAP-7781-22",
                    line_of_business="commercial_auto")
    session.add(policy)
    session.flush()
    return acme, other, policy


def test_an_exact_policy_number_scores_one(session):
    acme, _, policy = _seed(session)
    matches = rank_matches(session, extract_candidates(DEC))
    assert matches[0].score == 1.0
    assert matches[0].client_id == acme.id
    assert matches[0].policy_id == policy.id
    assert matches[0].reason == "exact policy number"


def test_a_similar_name_ranks_but_never_scores_one(session):
    """Name similarity alone must never reach the auto-link threshold."""
    _seed(session)
    text = DEC.replace("Policy Number: CAP-7781-22", "")
    matches = rank_matches(session, extract_candidates(text))
    assert matches
    assert all(m.score < 1.0 for m in matches)


def test_both_similarly_named_clients_are_offered(session):
    """She has to be able to see the near-miss, which is the whole point of
    showing candidates instead of an answer."""
    acme, other, _ = _seed(session)
    text = DEC.replace("Policy Number: CAP-7781-22", "")
    ids = {m.client_id for m in rank_matches(session, extract_candidates(text))}
    assert {acme.id, other.id} <= ids


def test_matches_are_ranked_and_limited(session):
    _seed(session)
    matches = rank_matches(session, extract_candidates(DEC), limit=1)
    assert len(matches) == 1


def test_nothing_recognisable_offers_nothing(session):
    _seed(session)
    assert rank_matches(session, extract_candidates("Dear customer,")) == []
