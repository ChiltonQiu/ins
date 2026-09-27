"""Trying proposed values before they are saved, and saying what went wrong.

Every function takes the values the person just typed, never the ones in
force, and returns a sentence for the page and the raw error for the log. A
failure nobody anticipated is shown as the raw error rather than a guess.
"""

from __future__ import annotations

import dataclasses
import smtplib
from dataclasses import dataclass

from renewal.config import Settings
from renewal.mail.imapbox import Mailbox, _connect
from renewal.notify import send_email
from renewal.providers import build_client, text_block

GMAIL_IMAP = "imap.gmail.com"
APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str
    detail: str = ""
    warning: bool = False


def scrub(text: str, secrets) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "•••")
    return text


def _unexpected(exc: Exception, secrets=()) -> str:
    # The only message built from the raw error, so the only one scrubbed:
    # scrubbing a sentence written here would mangle it for no benefit.
    return scrub(f"Something went wrong: {exc}", secrets)


# -------------------------------------------------------------------- model

def model_message(exc: Exception, secrets=()) -> str:
    import anthropic

    if isinstance(
        exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
    ):
        return ("That key didn't work. Copy it again from console.anthropic.com "
                "→ API keys, and paste the whole thing.")
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic. Is this computer online?"
    text = str(exc).lower()
    if "credit balance" in text or "billing" in text:
        return ("The key works, but the account has no credit. Add some at "
                "console.anthropic.com → Billing.")
    return _unexpected(exc, secrets)


def check_model(
    settings: Settings, key: str, *, build=build_client
) -> CheckResult:
    proposed = dataclasses.replace(
        settings, provider="anthropic", anthropic_api_key=key
    )
    try:
        build(proposed).complete(
            model=proposed.classification_model,
            system="Reply with the single word OK.",
            content=[text_block("Are you there?")],
        )
    except Exception as exc:  # noqa: BLE001 - every failure becomes a sentence
        return CheckResult(
            False, model_message(exc, [key]), scrub(repr(exc), [key])
        )
    return CheckResult(True, "Connected to Anthropic.")


# --------------------------------------------------------------------- mail

def mail_message(exc: Exception, host: str, secrets=()) -> str:
    text = str(exc).lower()
    gmail = host.lower() == GMAIL_IMAP
    if "not enabled for imap" in text or "imap access is disabled" in text:
        return ("IMAP is turned off for this account. In Gmail: Settings → See "
                "all settings → Forwarding and POP/IMAP → Enable IMAP.")
    if ("application-specific password" in text
            or "authenticationfailed" in text
            or "invalid credentials" in text
            or "authentication failed" in text
            or "login failed" in text):
        if gmail:
            return ("Gmail refused the login. It needs an app password, not the "
                    f"normal one — make one at {APP_PASSWORD_URL}")
        return ("The mail server refused the login. Check the address and "
                "password.")
    if isinstance(exc, OSError):
        return f"Couldn't reach {host}. Is this computer online?"
    return _unexpected(exc, secrets)


def list_folders(host, port, user, password, *, connector=None):
    box = Mailbox(host, user, password, port=port,
                  connector=connector or _connect)
    try:
        with box:
            folders = box.list_folders()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            False, mail_message(exc, host, [password]),
            scrub(repr(exc), [password]),
        ), []
    return CheckResult(True, f"Signed in. Found {len(folders)} folders."), folders


def check_mailbox(host, port, user, password, folder, *, connector=None):
    box = Mailbox(host, user, password, folder=folder, port=port,
                  connector=connector or _connect)
    try:
        with box:
            count = len(box.uids_since(None))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            False, mail_message(exc, host, [password]),
            scrub(repr(exc), [password]),
        ), 0
    if count == 0:
        return CheckResult(
            True,
            f"Connected. {folder} is empty — is the Gmail filter that files "
            "carrier mail there set up?",
            warning=True,
        ), 0
    return CheckResult(True, f"Connected. {count} messages in {folder}."), count


# ------------------------------------------------------------------ sending

def smtp_host_for(imap_host: str) -> str | None:
    return {GMAIL_IMAP: "smtp.gmail.com"}.get((imap_host or "").lower())


def check_smtp(settings, *, host, port, user, password, sender, to,
               send=send_email) -> CheckResult:
    proposed = dataclasses.replace(
        settings, smtp_host=host, smtp_port=port, smtp_username=user,
        smtp_password=password, notify_from=sender, notify_to=to,
    )
    try:
        send(
            "Renewal: test message",
            "This is the test message from Renewal's setup page. The daily "
            "summary will come from this address.",
            settings=proposed,
        )
    except smtplib.SMTPAuthenticationError as exc:
        who = "Gmail" if host == "smtp.gmail.com" else "The mail server"
        return CheckResult(
            False, f"{who} refused to send. Is it the same app password as "
                   "above?", scrub(repr(exc), [password]))
    except smtplib.SMTPException as exc:
        return CheckResult(
            False, scrub(f"The mail server refused: {exc}", [password]),
            scrub(repr(exc), [password]))
    except OSError as exc:
        return CheckResult(
            False, f"Couldn't reach {host}. Is this computer online?",
            scrub(repr(exc), [password]))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            False, _unexpected(exc, [password]),
            scrub(repr(exc), [password]))
    return CheckResult(True, f"Sent a test message to {to}.")
