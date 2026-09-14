"""Choosing the columns of a comparison.

The picker is how a renewal gets set against the quotes for the same risk.
Nothing on this page ranks the terms: they are listed in the order they were
written, and the broker decides which ones go side by side.
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.comparison import MAX_COLUMNS
from renewal.config import Settings
from renewal.models import Client, Comparison, ComparisonColumn, Policy, PolicyTerm
from renewal.web import create_app
from tests.authhelp import sign_in


@pytest.fixture
def app(engine, clean_db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root=tmp_path / "blobs",
        anthropic_api_key="unused",
        extraction_model="claude-opus-5",
        draft_model="claude-sonnet-5",
        confidence_threshold=0.80,
        materiality_config="config/materiality.yaml",
        session_cookie_secure=False,
    )
    return create_app(
        settings=settings,
        store=BlobStore(settings.blob_root),
        model_client=None,
        session_factory=sessionmaker(bind=engine),
    )


@pytest.fixture
def signed(app, engine):
    @contextmanager
    def _open():
        with TestClient(app) as test_client:
            sign_in(test_client, engine)
            yield test_client

    return _open


@pytest.fixture
def policy_id(engine):
    sess = sessionmaker(bind=engine)()
    client = Client(display_name="Ramirez Landscaping")
    sess.add(client)
    sess.flush()
    policy = Policy(
        client_id=client.id,
        carrier_name="Progressive",
        policy_number="AU-4471",
        line_of_business="commercial_auto",
        state="OR",
    )
    sess.add(policy)
    sess.commit()
    out = policy.id
    sess.close()
    yield out


def _term(engine, policy_id, *, premium, carrier="Progressive", kind="bound"):
    sess = sessionmaker(bind=engine)()
    term = PolicyTerm(
        policy_id=policy_id,
        kind=kind,
        carrier_name=carrier,
        policy_number="AU-4471",
        total_premium=premium,
    )
    sess.add(term)
    sess.commit()
    out = term.id
    sess.close()
    return out


def test_the_picker_lists_bound_and_quoted_terms_apart(signed, policy_id, engine):
    bound = _term(engine, policy_id, premium="3900.00")
    quote = _term(
        engine, policy_id, premium="3610.00", carrier="Cincinnati", kind="quoted"
    )
    with signed() as client:
        page = client.get(f"/policies/{policy_id}/compare").text

    assert "Progressive" in page and "Cincinnati" in page
    # The baseline can only be a bound term: measuring the incumbent against a
    # quote inverts what every other column means.
    assert f'name="baseline" value="{bound}"' in page
    assert f'name="baseline" value="{quote}"' not in page
    # Either may be a comparand.
    assert f'name="comparand" value="{bound}"' in page
    assert f'name="comparand" value="{quote}"' in page
    assert "up to four" in page


def test_preselection_ticks_the_terms_it_was_handed(signed, policy_id, engine):
    """The renewal_received attention item arrives here with both terms
    already chosen."""
    prior = _term(engine, policy_id, premium="3900.00")
    renewal = _term(engine, policy_id, premium="4210.00")
    with signed() as client:
        page = client.get(
            f"/policies/{policy_id}/compare",
            params={"baseline": prior, "comparand": renewal},
        ).text

    baseline_input = page[page.index(f'name="baseline" value="{prior}"') :][:200]
    assert "checked" in baseline_input
    comparand_input = page[page.index(f'name="comparand" value="{renewal}"') :][:200]
    assert "checked" in comparand_input


def test_a_baseline_and_two_comparands_build_a_three_column_comparison(
    signed, policy_id, engine
):
    bound = _term(engine, policy_id, premium="3900.00")
    first = _term(
        engine, policy_id, premium="3610.00", carrier="Cincinnati", kind="quoted"
    )
    second = _term(
        engine, policy_id, premium="4020.00", carrier="Sentry", kind="quoted"
    )
    with signed() as client:
        response = client.post(
            "/comparisons",
            data={
                "policy_id": str(policy_id),
                "baseline": str(bound),
                "comparand": [str(first), str(second)],
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    comparison_id = int(response.headers["location"].split("/")[-1])

    sess = sessionmaker(bind=engine)()
    columns = (
        sess.query(ComparisonColumn)
        .filter_by(comparison_id=comparison_id)
        .order_by(ComparisonColumn.position)
        .all()
    )
    assert [column.policy_term_id for column in columns] == [bound, first, second]
    assert [column.role for column in columns] == [
        "baseline",
        "comparand",
        "comparand",
    ]
    sess.close()


def test_no_draft_is_written_for_a_picked_comparison(signed, policy_id, engine):
    """A note that sets carriers side by side is a recommendation however it
    is worded. The renewal path in review.py is the one that drafts."""
    from renewal.models import Draft

    bound = _term(engine, policy_id, premium="3900.00")
    quote = _term(
        engine, policy_id, premium="3610.00", carrier="Cincinnati", kind="quoted"
    )
    with signed() as client:
        client.post(
            "/comparisons",
            data={
                "policy_id": str(policy_id),
                "baseline": str(bound),
                "comparand": [str(quote)],
            },
            follow_redirects=False,
        )

    sess = sessionmaker(bind=engine)()
    assert sess.query(Draft).count() == 0
    sess.close()


def test_a_quoted_baseline_comes_back_as_a_reason_not_a_traceback(
    signed, policy_id, engine
):
    quote = _term(
        engine, policy_id, premium="3610.00", carrier="Cincinnati", kind="quoted"
    )
    bound = _term(engine, policy_id, premium="3900.00")
    with signed() as client:
        response = client.post(
            "/comparisons",
            data={
                "policy_id": str(policy_id),
                "baseline": str(quote),
                "comparand": [str(bound)],
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "must be a bound term" in response.text

    sess = sessionmaker(bind=engine)()
    assert sess.query(Comparison).count() == 0
    sess.close()


def test_too_many_columns_comes_back_as_a_reason(signed, policy_id, engine):
    bound = _term(engine, policy_id, premium="3900.00")
    quotes = [
        _term(engine, policy_id, premium="3610.00", carrier=f"C{n}", kind="quoted")
        for n in range(MAX_COLUMNS)
    ]
    with signed() as client:
        response = client.post(
            "/comparisons",
            data={
                "policy_id": str(policy_id),
                "baseline": str(bound),
                "comparand": [str(term_id) for term_id in quotes],
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert f"at most {MAX_COLUMNS} columns" in response.text


def test_terms_of_another_policy_are_refused(signed, policy_id, engine):
    """The policy chain is what makes these the same risk."""
    sess = sessionmaker(bind=engine)()
    client_row = Client(display_name="Someone Else")
    sess.add(client_row)
    sess.flush()
    other = Policy(
        client_id=client_row.id,
        carrier_name="Progressive",
        policy_number="AU-9999",
        line_of_business="commercial_auto",
    )
    sess.add(other)
    sess.commit()
    other_id = other.id
    sess.close()

    bound = _term(engine, policy_id, premium="3900.00")
    stranger = _term(engine, other_id, premium="3610.00")
    with signed() as client:
        response = client.post(
            "/comparisons",
            data={
                "policy_id": str(policy_id),
                "baseline": str(bound),
                "comparand": [str(stranger)],
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "term of one policy" in response.text
