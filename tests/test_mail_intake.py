from datetime import datetime, timezone

from renewal.mail.intake import agency_for, receive
from renewal.mail.provider import Attachment, InboundEmail
from renewal.models import Agency, Document, DocumentText, InboundMessage
from tests.pdfmaker import make_text_pdf
from tests.test_dates_llm import StubClient, _settings


def _agency(session, address="intake+default@example.com"):
    """The agency row is configuration on a singleton, not a record of events.
    It is the documented exception to the insert-only rule, alongside the ics
    token: a rotated address must stop routing immediately, which an appended
    row would not achieve."""
    agency = session.query(Agency).filter_by(slug="default").one()
    agency.intake_address = address
    session.flush()
    return agency


# Generated once. make_text_pdf embeds a creation timestamp, so calling it
# twice produces different bytes and the dedupe test would be asserting on two
# attachments that are not in fact identical.
NOTICE_PDF = make_text_pdf([["NOTICE OF CANCELLATION"]])


def _email(*, to="intake+default@example.com", message_id="<a@b>", attach=True):
    return InboundEmail(
        message_id=message_id, from_address="underwriting@carrier.example",
        to_address=to, subject="Notice of cancellation",
        received_at=datetime.now(timezone.utc),
        body_text="Cancellation effective 07/01/2026.",
        raw_mime=b"raw mime bytes here",
        attachments=[Attachment("notice.pdf", "application/pdf", NOTICE_PDF)]
        if attach else [],
    )


def _receive(session, store, email):
    return receive(session, store, email,
                   client=StubClient('{"dates": []}'), settings=_settings())


def test_routing_is_by_recipient(session, store):
    agency = _agency(session)
    assert agency_for(session, "intake+default@example.com").id == agency.id


def test_an_unknown_recipient_is_quarantined_not_dropped(session, store):
    _agency(session)
    message = _receive(session, store, _email(to="intake+nobody@example.com"))
    assert message.processing_status == "quarantined"
    assert session.query(Document).count() == 0
    assert session.get(InboundMessage, message.id) is not None


def test_a_display_name_around_the_address_still_routes(session, store):
    _agency(session)
    message = _receive(session, store,
                       _email(to='"Intake" <intake+default@example.com>'))
    assert message.processing_status == "processed"


def test_the_body_becomes_a_document(session, store):
    """Deadlines are very often stated in prose in the body."""
    _agency(session)
    _receive(session, store, _email())
    bodies = session.query(Document).filter_by(source="email_body").all()
    assert len(bodies) == 1
    assert "Cancellation effective" in session.query(DocumentText).filter_by(
        document_id=bodies[0].id).one().text


def test_attachments_become_documents_linked_to_the_message(session, store):
    _agency(session)
    message = _receive(session, store, _email())
    attachments = session.query(Document).filter_by(
        source="email_attachment").all()
    assert len(attachments) == 1
    assert attachments[0].inbound_message_id == message.id


def test_a_message_with_no_attachment_still_produces_a_body_document(session, store):
    _agency(session)
    _receive(session, store, _email(attach=False))
    assert session.query(Document).filter_by(source="email_body").count() == 1


def test_a_duplicate_message_id_does_no_work(session, store):
    """Forwarded mail arrives multiple times. The second arrival returns the
    row we already have and creates nothing."""
    _agency(session)
    first = _receive(session, store, _email())
    second = _receive(session, store, _email())
    assert second.id == first.id
    assert session.query(InboundMessage).count() == 1
    assert session.query(Document).count() == 2


def test_identical_attachments_across_messages_share_one_blob(session, store):
    _agency(session)
    _receive(session, store, _email(message_id="<one@x>"))
    _receive(session, store, _email(message_id="<two@x>"))
    digests = {d.blob_sha256 for d in session.query(Document).filter_by(
        source="email_attachment")}
    assert len(digests) == 1


def test_the_raw_mime_is_stored_as_a_blob(session, store):
    _agency(session)
    message = _receive(session, store, _email())
    assert store.get(message.raw_mime_blob_sha256, ext="eml") == b"raw mime bytes here"


def test_a_non_pdf_attachment_is_skipped_without_failing_the_message(session, store):
    _agency(session)
    email = _email()
    email.attachments.append(Attachment("logo.png", "image/png", b"\x89PNG..."))
    message = _receive(session, store, email)
    assert message.processing_status == "processed"
    assert session.query(Document).filter_by(source="email_attachment").count() == 1


def test_an_email_body_is_never_picked_up_by_the_structured_extractor(session, store):
    """doc_type separates it from the dec_page marker that path looks for."""
    _agency(session)
    _receive(session, store, _email())
    body = session.query(Document).filter_by(source="email_body").one()
    assert body.doc_type == "email_body"
