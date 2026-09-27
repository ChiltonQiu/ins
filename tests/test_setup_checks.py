"""Each failure somebody will actually hit, as the sentence they will read."""

import dataclasses
import imaplib
import smtplib
import socket
from pathlib import Path

import anthropic
import httpx

from renewal.config import Settings
from renewal.setup import checks
from renewal.setup.status import status_of


def _settings(**kw):
    base = Settings(
        database_url="postgresql+psycopg:///x", blob_root=Path("b"),
        anthropic_api_key="", extraction_model="m", draft_model="m",
        confidence_threshold=0.8, materiality_config=Path("m.yaml"),
    )
    return dataclasses.replace(base, **kw)


def _raising(exc):
    class Client:
        def complete(self, **kw):
            raise exc
    return lambda settings: Client()


_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_a_working_key():
    class Client:
        def complete(self, **kw):
            return "OK"
    result = checks.check_model(_settings(), "sk", build=lambda s: Client())
    assert result.ok


def test_a_rejected_key():
    exc = anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx.Response(401, request=_REQ), body=None)
    result = checks.check_model(_settings(), "sk", build=_raising(exc))
    assert not result.ok
    assert "didn't work" in result.message and "console.anthropic.com" in result.message


def test_no_credit():
    exc = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=httpx.Response(400, request=_REQ), body=None)
    result = checks.check_model(_settings(), "sk", build=_raising(exc))
    assert "no credit" in result.message


def test_no_network_to_anthropic():
    exc = anthropic.APIConnectionError(request=_REQ)
    result = checks.check_model(_settings(), "sk", build=_raising(exc))
    assert "Couldn't reach Anthropic" in result.message


def test_an_unknown_model_error_is_shown_not_hidden():
    result = checks.check_model(_settings(), "sk", build=_raising(RuntimeError("weird")))
    assert result.message.startswith("Something went wrong") and "weird" in result.message


def test_the_key_never_reaches_the_logged_detail():
    result = checks.check_model(
        _settings(), "sk-SECRET", build=_raising(RuntimeError("bad key sk-SECRET")))
    assert "sk-SECRET" not in result.detail


class FakeImap:
    def __init__(self, *, login_error=None, folders=(), uids=b""):
        self.login_error, self.folders, self.uids = login_error, folders, uids

    def login(self, user, password):
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return ("OK", [b"ok"])

    def examine(self, folder):
        return ("OK", [b"1"])

    def list(self):
        return ("OK", list(self.folders))

    def uid(self, command, *args):
        return ("OK", [self.uids])

    def logout(self):
        return ("BYE", [])


def test_gmail_refusing_a_normal_password_asks_for_an_app_password():
    fake = FakeImap(login_error=b"[ALERT] Application-specific password required")
    result, folders = checks.list_folders(
        "imap.gmail.com", 993, "her@gmail.com", "pw", connector=lambda h, p: fake)
    assert not result.ok and folders == []
    assert "app password" in result.message


def test_wrong_credentials_say_app_password_on_gmail():
    fake = FakeImap(login_error=b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    result, _ = checks.list_folders(
        "imap.gmail.com", 993, "u", "p", connector=lambda h, p: fake)
    assert "Gmail refused the login" in result.message


def test_wrong_credentials_elsewhere_do_not_mention_gmail():
    fake = FakeImap(login_error=b"[AUTHENTICATIONFAILED] Invalid credentials")
    result, _ = checks.list_folders(
        "mail.example.com", 993, "u", "p", connector=lambda h, p: fake)
    assert "Gmail" not in result.message and "refused the login" in result.message


def test_imap_turned_off():
    fake = FakeImap(login_error=b"[ALERT] Your account is not enabled for IMAP use.")
    result, _ = checks.list_folders(
        "imap.gmail.com", 993, "u", "p", connector=lambda h, p: fake)
    assert "IMAP is turned off" in result.message


def test_host_unreachable():
    def connector(h, p):
        raise socket.gaierror(11001, "getaddrinfo failed")
    result, _ = checks.list_folders("imap.nowhere.example", 993, "u", "p", connector=connector)
    assert "Couldn't reach imap.nowhere.example" in result.message


def test_folders_are_returned():
    fake = FakeImap(folders=[b'(\\HasNoChildren) "/" "INBOX"',
                             b'(\\HasNoChildren) "/" "Carriers"'])
    result, folders = checks.list_folders(
        "imap.gmail.com", 993, "u", "p", connector=lambda h, p: fake)
    assert result.ok and folders == ["INBOX", "Carriers"]


def test_a_mailbox_with_mail():
    fake = FakeImap(uids=b"1 2 3")
    result, count = checks.check_mailbox(
        "imap.gmail.com", 993, "u", "p", "Carriers", connector=lambda h, p: fake)
    assert result.ok and not result.warning and count == 3
    assert "3 messages in Carriers" in result.message


def test_an_empty_folder_is_a_warning_not_a_failure():
    fake = FakeImap(uids=b"")
    result, count = checks.check_mailbox(
        "imap.gmail.com", 993, "u", "p", "Carriers", connector=lambda h, p: fake)
    assert result.ok and result.warning and count == 0
    assert "filter" in result.message


def test_smtp_auth_refused():
    def send(subject, body, *, settings):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    result = checks.check_smtp(
        _settings(), host="smtp.gmail.com", port=587, user="u", password="p",
        sender="u", to="t@x.com", send=send)
    assert "refused to send" in result.message


def test_smtp_unreachable():
    def send(subject, body, *, settings):
        raise ConnectionRefusedError(111, "refused")
    result = checks.check_smtp(
        _settings(), host="smtp.example.com", port=587, user="u", password="p",
        sender="u", to="t@x.com", send=send)
    assert "Couldn't reach smtp.example.com" in result.message


def test_smtp_sends():
    sent = []
    result = checks.check_smtp(
        _settings(), host="smtp.gmail.com", port=587, user="u", password="p",
        sender="u@gmail.com", to="t@x.com",
        send=lambda subject, body, *, settings: sent.append(settings))
    assert result.ok and sent[0].smtp_host == "smtp.gmail.com"
    assert sent[0].notify_to == "t@x.com"


def test_gmail_sends_through_gmail():
    assert checks.smtp_host_for("imap.gmail.com") == "smtp.gmail.com"
    assert checks.smtp_host_for("mail.example.com") is None


def test_secrets_are_scrubbed_from_logged_detail():
    assert checks.scrub("login p@ss failed for p@ss", ["p@ss", ""]) == "login ••• failed for •••"


def test_status_counts_the_two_required_steps():
    from renewal.providers import UnconfiguredClient
    none = status_of(_settings(), UnconfiguredClient("x"))
    assert (none.done, none.complete) == (0, False)
    one = status_of(_settings(anthropic_api_key="k"), object())
    assert one.done == 1
    both = status_of(
        _settings(imap_host="h", imap_user="u", imap_password="p"), object())
    assert (both.done, both.complete) == (2, True)
