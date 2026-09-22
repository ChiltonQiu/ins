"""The front page.

It reports rather than decides, so what is tested here is that the numbers
come from the same place the pages they link to come from — a second count
written for the dashboard is the one that would eventually be wrong, and
nobody would notice.
"""

from datetime import date, timedelta

from sqlalchemy.orm import sessionmaker

from renewal.blobstore import BlobStore
from renewal.ingest import ingest_pdf
from renewal.models import Client, Comparison, ManualDate, Policy, PolicyTerm
from tests.pdfmaker import make_text_pdf
from tests.test_web_inbox import app, settings, signed  # noqa: F401


def test_the_front_page_is_not_the_inbox(signed):
    """They were the same URL until the dashboard existed. A test, because
    every redirect in the application had to move with it."""
    with signed() as client:
        home = client.get("/")
        inbox = client.get("/inbox")
    assert home.status_code == 200
    assert inbox.status_code == 200
    assert "Renewals it built" in home.text
    assert "Renewals it built" not in inbox.text


def test_a_document_that_needs_her_is_counted_and_shown(signed, engine, settings):
    session = sessionmaker(bind=engine)()
    try:
        document = ingest_pdf(
            session, BlobStore(settings.blob_root),
            data=make_text_pdf([["Named Insured: Nobody In Particular"]]),
            original_filename="mystery.pdf", source="manual_upload", agency_id=1,
        )
        session.commit()
        document_id = document.id
    finally:
        session.close()

    with signed() as client:
        client.post(f"/documents/{document_id}/retry", follow_redirects=False)
        page = client.get("/").text

    assert "mystery.pdf" in page
    assert "needs client" in page


def test_an_unconnected_mailbox_says_so_on_the_front_page(signed):
    """The failure this exists to catch: a quiet week and a forwarding rule
    that broke on Thursday look identical from every other panel."""
    with signed() as client:
        assert "No mailbox is connected" in client.get("/").text


def test_a_renewal_comparison_reports_its_premium_change(signed, engine):
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Ramirez Landscaping")
        session.add(client_row)
        session.flush()
        policy = Policy(client_id=client_row.id, carrier_name="Travelers",
                        policy_number="TR-9001", line_of_business="property")
        session.add(policy)
        session.flush()
        prior = PolicyTerm(policy_id=policy.id, carrier_name="Travelers",
                           policy_number="TR-9001", total_premium="10000")
        renewal = PolicyTerm(policy_id=policy.id, carrier_name="Travelers",
                             policy_number="TR-9001", total_premium="11500")
        session.add_all([prior, renewal])
        session.flush()
        session.add(Comparison(prior_term_id=prior.id, renewal_term_id=renewal.id))
        session.commit()
    finally:
        session.close()

    with signed() as client:
        page = client.get("/").text

    assert "Ramirez Landscaping" in page
    assert "+1,500" in page
    assert "+15%" in page


def test_a_premium_that_does_not_parse_reports_nothing_rather_than_zero(
    signed, engine
):
    """A premium is the text the document showed. 'see schedule' is not a
    number, and a dashboard that renders it as a 100% decrease is worse than
    one that says nothing."""
    session = sessionmaker(bind=engine)()
    try:
        client_row = Client(display_name="Delgado Auto Body")
        session.add(client_row)
        session.flush()
        policy = Policy(client_id=client_row.id, carrier_name="Acuity",
                        policy_number="AC-22", line_of_business="commercial auto")
        session.add(policy)
        session.flush()
        prior = PolicyTerm(policy_id=policy.id, total_premium="see schedule")
        renewal = PolicyTerm(policy_id=policy.id, total_premium="8000")
        session.add_all([prior, renewal])
        session.flush()
        session.add(Comparison(prior_term_id=prior.id, renewal_term_id=renewal.id))
        session.commit()
    finally:
        session.close()

    with signed() as client:
        page = client.get("/").text

    assert "Delgado Auto Body" in page
    assert "%" not in page.split("Delgado Auto Body")[1].split("</tr>")[0]


def test_a_date_inside_the_window_is_shown_and_one_outside_it_is_not(
    signed, engine
):
    today = date.today()
    session = sessionmaker(bind=engine)()
    try:
        session.add(ManualDate(
            agency_id=1, title="audit walkthrough",
            date_value=today + timedelta(days=9), date_type="audit_date",
            created_by="human"))
        session.add(ManualDate(
            agency_id=1, title="far future thing",
            date_value=today + timedelta(days=200), date_type="other",
            created_by="human"))
        session.commit()
    finally:
        session.close()

    with signed() as client:
        page = client.get("/").text

    assert "audit walkthrough" in page
    assert "far future thing" not in page
