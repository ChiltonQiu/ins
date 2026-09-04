from email.message import EmailMessage

from renewal.mail.filedrop import FileDropProvider
from renewal.mail.parse import parse_mime
from tests.pdfmaker import make_text_pdf


def _message(*, with_attachment=True, html_only=False):
    message = EmailMessage()
    message["Message-ID"] = "<abc123@carrier.example>"
    message["From"] = "underwriting@carrier.example"
    message["To"] = "intake+default@example.com"
    message["Subject"] = "Notice of cancellation - Acme Landscaping"
    message["Date"] = "Mon, 01 Jun 2026 09:00:00 -0700"
    if html_only:
        message.set_content("<p>Cancellation effective 07/01/2026.</p>",
                            subtype="html")
    else:
        message.set_content("Cancellation effective 07/01/2026.")
    if with_attachment:
        message.add_attachment(make_text_pdf([["NOTICE OF CANCELLATION"]]),
                               maintype="application", subtype="pdf",
                               filename="notice.pdf")
    return message.as_bytes()


def test_headers_are_read():
    got = parse_mime(_message())
    assert got.message_id == "<abc123@carrier.example>"
    assert got.to_address == "intake+default@example.com"
    assert got.subject.startswith("Notice of cancellation")


def test_the_body_text_is_extracted():
    """The carrier's explanation is frequently in the body while the
    attachment is a bare form."""
    assert "Cancellation effective 07/01/2026" in parse_mime(_message()).body_text


def test_an_html_only_body_is_reduced_to_text():
    body = parse_mime(_message(html_only=True)).body_text
    assert "Cancellation effective 07/01/2026" in body
    assert "<p>" not in body


def test_pdf_attachments_are_extracted():
    attachments = parse_mime(_message()).attachments
    assert [a.filename for a in attachments] == ["notice.pdf"]
    assert attachments[0].data.startswith(b"%PDF")


def test_a_message_with_no_attachment_still_parses():
    got = parse_mime(_message(with_attachment=False))
    assert got.attachments == []
    assert got.body_text


def test_the_raw_mime_is_preserved_verbatim():
    raw = _message()
    assert parse_mime(raw).raw_mime == raw


def test_a_missing_message_id_is_synthesised_from_the_content():
    """Dedupe needs a key. A content hash is stable across re-deliveries of the
    same message, which is exactly the property Message-ID was providing."""
    message = EmailMessage()
    message["From"] = "a@b.example"
    message["To"] = "intake+default@example.com"
    message.set_content("no message id here")
    got = parse_mime(message.as_bytes())
    assert got.message_id.startswith("sha256:")


def test_the_received_date_falls_back_to_now_when_unparseable():
    message = EmailMessage()
    message["Message-ID"] = "<x@y>"
    message["From"] = "a@b.example"
    message["To"] = "intake+default@example.com"
    message["Date"] = "not a date"
    message.set_content("body")
    assert parse_mime(message.as_bytes()).received_at is not None


def test_the_filedrop_provider_reads_an_eml_file(tmp_path):
    path = tmp_path / "one.eml"
    path.write_bytes(_message())
    provider = FileDropProvider(tmp_path)
    assert [e.message_id for e in provider.drain()] == ["<abc123@carrier.example>"]


def test_the_filedrop_provider_verifies_nothing_and_says_so(tmp_path):
    assert FileDropProvider(tmp_path).verify({}, b"") is True
