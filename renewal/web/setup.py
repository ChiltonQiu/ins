"""/setup: the key, the mailbox and the sender, from a page.

Each card tries the values somebody just typed before saving them, and says in
one sentence what went wrong when they do not work. What it saves goes to
.env and nowhere else (renewal/envfile.py), and Live picks it up at once, so
nothing is restarted. A saved secret is never rendered back: the field comes
back empty, and empty means "keep what is saved".

The mailbox is two steps so that the password is typed once. Signing in saves
the login and lists the folders, with IMAP_HOST cleared so nothing polls
INBOX in between; choosing a folder then sets the host and turns polling on.
"""

from __future__ import annotations

import dataclasses
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from renewal import envfile, settings_store
from renewal.mail.poll import start_from_beginning, start_from_now
from renewal.models import MailPollState
from renewal.providers import is_configured
from renewal.settings_store import effective
from renewal.setup import checks
from renewal.setup.checks import CheckResult
from renewal.setup.status import status_of
from renewal.web.deps import Deps
from renewal.web.templating import TEMPLATES

logger = logging.getLogger(__name__)

EXTERNAL = ("This is set by the server's environment — change it there. "
            "Anything typed here would be ignored.")
GMAIL_IMAP = checks.GMAIL_IMAP
NEEDS_AI = CheckResult(False, "Set up the AI first — mail read without it is "
                              "stored but not read.")
MAIL_KEYS = frozenset({"IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD",
                       "IMAP_FOLDER"})
SUMMARY_KEYS = frozenset({"SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
                          "SMTP_PASSWORD", "NOTIFY_FROM"})


def _port(raw: str, default: int) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return default
    return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else None


def register(app, deps: Deps) -> None:
    router = APIRouter()
    session_factory = deps.session_factory

    def _render(request: Request, *, results=None, failed=(), folders=None,
                pending=None, form=None, status_code=200):
        live_settings = deps.settings
        with session_factory() as session:
            current = effective(session, live_settings)
            poll = None
            if current.imap_host:
                poll = session.scalar(
                    select(MailPollState)
                    .where(MailPollState.host == current.imap_host)
                    .where(MailPollState.folder == current.imap_folder)
                )
                if poll is not None:
                    session.expunge(poll)
        suggested_smtp = checks.smtp_host_for(current.imap_host)
        same_account = (
            not current.smtp_host or current.smtp_host == suggested_smtp
        )
        return TEMPLATES.TemplateResponse(
            request,
            "setup.html",
            {
                "status": status_of(current, deps.model_client),
                "s": current,
                "saved": {
                    "key": bool(current.anthropic_api_key),
                    "imap_password": bool(current.imap_password),
                    "smtp_password": bool(current.smtp_password),
                },
                "external": {
                    "ai": "ANTHROPIC_API_KEY" in deps.live.external_keys,
                    "mail": bool(deps.live.external_keys & MAIL_KEYS),
                    "summary": bool(deps.live.external_keys & SUMMARY_KEYS),
                },
                "other_provider": current.provider != "anthropic",
                "results": results or {},
                "failed": set(failed),
                "folders": folders,
                "pending": pending or {},
                "form": form or {},
                "poll": poll,
                "same_account": same_account,
                "gmail_imap": GMAIL_IMAP,
                "app_password_url": checks.APP_PASSWORD_URL,
                "just_saved": request.query_params.get("saved"),
                "update": None,
            },
            status_code=status_code,
        )

    def _save(updates: dict[str, str]) -> None:
        envfile.write(deps.live.env_path, updates)
        deps.live.reload()

    def _external(*keys: str) -> bool:
        return any(key in deps.live.external_keys for key in keys)

    def _log(card: str, result: CheckResult) -> None:
        logger.warning("setup check failed card=%s detail=%s", card, result.detail)

    # ----------------------------------------------------------------- page

    @router.get("/setup", response_class=HTMLResponse)
    def show_setup(request: Request):
        return _render(request)

    # ------------------------------------------------------------------- AI

    @router.post("/setup/model", response_class=HTMLResponse)
    def save_model(request: Request, api_key: str = Form(""),
                   action: str = Form("test")):
        if _external("ANTHROPIC_API_KEY"):
            return _render(request, results={"ai": CheckResult(False, EXTERNAL)})
        key = api_key.strip() or deps.settings.anthropic_api_key
        if not key:
            return _render(request, results={
                "ai": CheckResult(False, "Paste the key first.")})
        if action != "save_anyway":
            result = checks.check_model(deps.settings, key)
            if not result.ok:
                _log("ai", result)
                return _render(request, results={"ai": result}, failed={"ai"})
        updates = {"ANTHROPIC_API_KEY": key}
        if not _external("PROVIDER"):
            updates["PROVIDER"] = "anthropic"
        _save(updates)
        return RedirectResponse("/setup?saved=ai#ai", status_code=303)

    # -------------------------------------------------------- mail, step one

    @router.post("/setup/mail/folders", response_class=HTMLResponse)
    def list_mail_folders(request: Request, email: str = Form(""),
                          password: str = Form(""),
                          host: str = Form(GMAIL_IMAP), port: str = Form("993")):
        email, host = email.strip(), (host.strip() or GMAIL_IMAP)
        pending = {"email": email, "host": host, "port": port}
        if not is_configured(deps.model_client):
            return _render(request, results={"mail": NEEDS_AI}, form=pending)
        if _external("IMAP_USER", "IMAP_PASSWORD", "IMAP_HOST"):
            return _render(request, results={"mail": CheckResult(False, EXTERNAL)},
                           form=pending)
        number = _port(port, 993)
        password = password or deps.settings.imap_password
        problem = (
            "Type the email address first." if not email
            else "Type the app password first." if not password
            else "The port should be a number, usually 993." if number is None
            else None
        )
        if problem:
            return _render(request, results={"mail": CheckResult(False, problem)},
                           form=pending)
        result, folders = checks.list_folders(host, number, email, password)
        if not result.ok:
            _log("mail", result)
            return _render(request, results={"mail": result}, form=pending)
        # The login is kept so it is typed once; the host is cleared so that
        # nothing polls until a folder has been chosen.
        _save({"IMAP_USER": email, "IMAP_PASSWORD": password, "IMAP_HOST": ""})
        return _render(request, results={"mail": result}, folders=folders,
                       pending={"host": host, "port": str(number)})

    # -------------------------------------------------------- mail, step two

    @router.post("/setup/mail", response_class=HTMLResponse)
    def save_mail(request: Request, folder: str = Form(""), start: str = Form("new"),
                  host: str = Form(GMAIL_IMAP), port: str = Form("993"),
                  action: str = Form("test")):
        pending = {"host": host, "port": port}
        if not is_configured(deps.model_client):
            return _render(request, results={"mail": NEEDS_AI})
        if _external("IMAP_HOST", "IMAP_PORT", "IMAP_FOLDER"):
            return _render(request, results={"mail": CheckResult(False, EXTERNAL)})
        user, password = deps.settings.imap_user, deps.settings.imap_password
        number = _port(port, 993)
        if not (user and password):
            return _render(request, results={"mail": CheckResult(
                False, "Sign in to the mailbox first.")})
        if not folder or number is None:
            return _render(request, results={"mail": CheckResult(
                False, "Choose a folder first.")})
        proposed = dataclasses.replace(
            deps.settings, imap_host=host, imap_port=number, imap_folder=folder)
        if action != "save_anyway":
            result, _count = checks.check_mailbox(host, number, user, password, folder)
            if not result.ok:
                _log("mail", result)
                return _render(request, results={"mail": result}, failed={"mail"},
                               folders=[folder], pending=pending)
        try:
            with session_factory() as session:
                if start == "all":
                    start_from_beginning(session, proposed)
                else:
                    start_from_now(session, proposed)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - said on the page, not raised
            logger.warning("setup could not mark the folder: %r", exc)
            return _render(request, results={"mail": CheckResult(
                False, "Couldn't mark what is already in the folder, so nothing "
                       f"was saved. {checks.mail_message(exc, host, [password])}")},
                folders=[folder], pending=pending)
        _save({"IMAP_HOST": host, "IMAP_PORT": str(number), "IMAP_FOLDER": folder})
        return RedirectResponse("/setup?saved=mail#mail", status_code=303)

    # --------------------------------------------------------------- summary

    @router.post("/setup/summary", response_class=HTMLResponse)
    def save_summary(request: Request, to: str = Form(""),
                     same_account: str = Form(""), host: str = Form(""),
                     port: str = Form("587"), user: str = Form(""),
                     password: str = Form(""), action: str = Form("test")):
        to = to.strip()
        form = {"to": to, "host": host, "port": port, "user": user,
                "same_account": bool(same_account)}
        if _external("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                     "NOTIFY_FROM"):
            return _render(request, results={"summary": CheckResult(False, EXTERNAL)},
                           form=form)
        if not to:
            _save({"SMTP_HOST": ""})
            with session_factory() as session:
                settings_store.write(session, "notify_to", "")
                session.commit()
            return RedirectResponse("/setup?saved=summary#summary", status_code=303)

        settings = deps.settings
        if same_account:
            host = checks.smtp_host_for(settings.imap_host) or ""
            number, user, password = 587, settings.imap_user, settings.imap_password
            if not host:
                return _render(request, results={"summary": CheckResult(
                    False, "Not Gmail? Untick 'same account' and fill in the "
                           "server.")}, form=form)
            if not (user and password):
                return _render(request, results={"summary": CheckResult(
                    False, "Set up reading mail first, or untick 'same account'.")},
                    form=form)
        else:
            host, user = host.strip(), user.strip()
            number = _port(port, 587)
            password = password or settings.smtp_password
            if not (host and user and password) or number is None:
                return _render(request, results={"summary": CheckResult(
                    False, "Fill in the server, port, address and password.")},
                    form=form)

        try:
            settings_store.parse(settings_store.BY_KEY["notify_to"], to)
        except settings_store.Invalid as exc:
            return _render(request, results={"summary": CheckResult(
                False, str(exc))}, form=form)

        if action != "save_anyway":
            result = checks.check_smtp(settings, host=host, port=number, user=user,
                                       password=password, sender=user, to=to)
            if not result.ok:
                _log("summary", result)
                return _render(request, results={"summary": result},
                               failed={"summary"}, form=form)
        _save({"SMTP_HOST": host, "SMTP_PORT": str(number), "SMTP_USERNAME": user,
               "SMTP_PASSWORD": password, "NOTIFY_FROM": user})
        with session_factory() as session:
            settings_store.write(session, "notify_to", to)
            session.commit()
        return RedirectResponse("/setup?saved=summary#summary", status_code=303)

    app.include_router(router)
