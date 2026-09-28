"""The setup page, against a real .env in a temporary directory."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from renewal import envfile
from renewal.blobstore import BlobStore
from renewal.live import Live
from renewal.setup.checks import CheckResult
from renewal.web import create_app
from tests.authhelp import EMAIL, PASSWORD, sign_in
from tests.pdfmaker import make_text_pdf

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    saved = dict(os.environ)
    path = tmp_path / ".env"
    path.write_text(
        "DATABASE_URL=postgresql+psycopg:///renewal_test\n"
        f"BLOB_ROOT={tmp_path / 'blobs'}\n"
        "SESSION_COOKIE_SECURE=false\n"
    )
    monkeypatch.setenv("RENEWAL_ENV_FILE", str(path))
    for key in envfile.OWNED:
        monkeypatch.delenv(key, raising=False)
    yield path
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def live(env_path):
    return Live(env_path=env_path, external=frozenset())


@pytest.fixture
def web(engine, clean_db, live, tmp_path):
    app = create_app(
        store=BlobStore(tmp_path / "blobs"),
        session_factory=sessionmaker(bind=engine), live=live,
    )
    with TestClient(app) as client:
        sign_in(client, engine)
        yield client


def ok(message="fine", **kw):
    return CheckResult(True, message, **kw)


def bad(message="nope"):
    return CheckResult(False, message)


def test_the_page_renders_with_nothing_set_up(web):
    page = web.get("/setup")
    assert page.status_code == 200
    assert "Not set up" in page.text


def test_other_pages_carry_the_banner_until_setup_is_done(web):
    assert "Finish setup (0 of 2 done)" in web.get("/inbox").text
    assert "Finish setup" not in web.get("/setup").text


def test_signing_in_lands_on_setup_while_it_is_unfinished(web):
    web.post("/logout", headers=ORIGIN)
    response = web.post("/login", data={"email": EMAIL, "password": PASSWORD},
                        headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/setup"


def test_a_working_key_is_saved_and_takes_effect(web, live, env_path, monkeypatch):
    monkeypatch.setattr("renewal.setup.checks.check_model", lambda s, k, **kw: ok())
    response = web.post("/setup/model", data={"api_key": " sk-good ", "action": "test"},
                        headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert envfile.read(env_path)["ANTHROPIC_API_KEY"] == "sk-good"
    assert live.settings.anthropic_api_key == "sk-good"
    assert "Finish setup (1 of 2 done)" in web.get("/inbox").text


def test_a_failing_key_is_not_saved_and_offers_save_anyway(web, env_path, monkeypatch):
    monkeypatch.setattr("renewal.setup.checks.check_model",
                        lambda s, k, **kw: bad("That key didn't work."))
    page = web.post("/setup/model", data={"api_key": "sk-bad", "action": "test"},
                    headers=ORIGIN)
    assert "That key didn&#39;t work." in page.text and "Save anyway" in page.text
    assert "ANTHROPIC_API_KEY" not in envfile.read(env_path)


def test_save_anyway_saves_without_checking(web, env_path, monkeypatch):
    def never(*a, **k):
        raise AssertionError("checked")
    monkeypatch.setattr("renewal.setup.checks.check_model", never)
    web.post("/setup/model", data={"api_key": "sk-x", "action": "save_anyway"},
             headers=ORIGIN)
    assert envfile.read(env_path)["ANTHROPIC_API_KEY"] == "sk-x"


def test_a_blank_key_keeps_the_saved_one(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk-kept"})
    live.reload()
    seen = []
    monkeypatch.setattr("renewal.setup.checks.check_model",
                        lambda s, k, **kw: seen.append(k) or ok())
    web.post("/setup/model", data={"api_key": "", "action": "test"}, headers=ORIGIN)
    assert seen == ["sk-kept"]


def test_a_blank_key_with_nothing_saved_is_refused(web, env_path):
    page = web.post("/setup/model", data={"api_key": "", "action": "test"},
                    headers=ORIGIN)
    assert "Paste the key first" in page.text


def test_no_secret_ever_reaches_a_page(web, env_path, live):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk-SECRET-1",
                             "IMAP_PASSWORD": "pw-SECRET-2",
                             "IMAP_USER": "her@gmail.com"})
    live.reload()
    response = web.get("/setup")
    assert response.status_code == 200
    assert "SECRET" not in response.text and "saved" in response.text


def test_listing_folders_saves_the_login_but_keeps_polling_off(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk"})
    live.reload()
    monkeypatch.setattr("renewal.setup.checks.list_folders",
                        lambda *a, **k: (ok(), ["INBOX", "Carriers"]))
    page = web.post("/setup/mail/folders", data={
        "email": "her@gmail.com", "password": "app pass word",
        "host": "imap.gmail.com", "port": "993"}, headers=ORIGIN)
    assert '<option value="Carriers"' in page.text
    assert "app pass word" not in page.text
    saved = envfile.read(env_path)
    assert (saved["IMAP_USER"], saved["IMAP_PASSWORD"], saved["IMAP_HOST"]) == (
        "her@gmail.com", "app pass word", "")
    assert live.settings.imap_host == ""


def test_a_failed_folder_listing_saves_nothing(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk"})
    live.reload()
    monkeypatch.setattr("renewal.setup.checks.list_folders",
                        lambda *a, **k: (bad("Gmail refused the login."), []))
    page = web.post("/setup/mail/folders", data={
        "email": "her@gmail.com", "password": "wrong",
        "host": "imap.gmail.com", "port": "993"}, headers=ORIGIN)
    assert "Gmail refused the login." in page.text
    assert "IMAP_PASSWORD" not in envfile.read(env_path)


def test_signing_in_to_the_mailbox_needs_the_ai_first(web, env_path, monkeypatch):
    def never(*a, **k):
        raise AssertionError("listed")
    monkeypatch.setattr("renewal.setup.checks.list_folders", never)
    page = web.post("/setup/mail/folders", data={
        "email": "her@gmail.com", "password": "pw",
        "host": "imap.gmail.com", "port": "993"}, headers=ORIGIN)
    assert "Set up the AI first" in page.text
    assert "IMAP_PASSWORD" not in envfile.read(env_path)


def test_choosing_a_folder_needs_the_ai_first(web):
    page = web.post("/setup/mail", data={
        "folder": "Carriers", "start": "new", "host": "imap.gmail.com",
        "port": "993", "action": "test"}, headers=ORIGIN)
    assert "Set up the AI first" in page.text


def test_choosing_a_folder_from_now_on(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk", "IMAP_USER": "u",
                             "IMAP_PASSWORD": "p"})
    live.reload()
    monkeypatch.setattr("renewal.setup.checks.check_mailbox", lambda *a, **k: (ok(), 3))
    started = []
    monkeypatch.setattr("renewal.web.setup.start_from_now",
                        lambda session, settings: started.append(settings.imap_folder) or 3)
    response = web.post("/setup/mail", data={
        "folder": "Carriers", "start": "new", "host": "imap.gmail.com",
        "port": "993", "action": "test"}, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert started == ["Carriers"]
    assert live.settings.imap_host == "imap.gmail.com"
    assert live.settings.imap_folder == "Carriers"
    assert "Finish setup" not in web.get("/inbox").text


def test_choosing_everything_reads_the_whole_folder(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk", "IMAP_USER": "u",
                             "IMAP_PASSWORD": "p"})
    live.reload()
    monkeypatch.setattr("renewal.setup.checks.check_mailbox", lambda *a, **k: (ok(), 3))
    reset = []
    monkeypatch.setattr("renewal.web.setup.start_from_beginning",
                        lambda session, settings: reset.append(settings.imap_folder))
    web.post("/setup/mail", data={
        "folder": "Carriers", "start": "all", "host": "imap.gmail.com",
        "port": "993", "action": "test"}, headers=ORIGIN)
    assert reset == ["Carriers"]


def test_the_summary_can_use_the_same_gmail_account(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"IMAP_HOST": "imap.gmail.com",
                             "IMAP_USER": "her@gmail.com", "IMAP_PASSWORD": "pw"})
    live.reload()
    seen = {}
    monkeypatch.setattr("renewal.setup.checks.check_smtp",
                        lambda settings, **kw: seen.update(kw) or ok())
    web.post("/setup/summary", data={"to": "her@gmail.com", "same_account": "on",
                                     "action": "test"}, headers=ORIGIN)
    assert (seen["host"], seen["user"], seen["password"]) == (
        "smtp.gmail.com", "her@gmail.com", "pw")
    saved = envfile.read(env_path)
    assert saved["SMTP_HOST"] == "smtp.gmail.com"
    assert saved["NOTIFY_FROM"] == "her@gmail.com"


def test_a_blank_summary_address_turns_sending_off(web, env_path, live):
    envfile.write(env_path, {"SMTP_HOST": "smtp.gmail.com"})
    live.reload()
    web.post("/setup/summary", data={"to": "", "action": "test"}, headers=ORIGIN)
    assert envfile.read(env_path)["SMTP_HOST"] == ""


def test_a_key_from_the_environment_is_not_offered(engine, clean_db, env_path, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-outside")
    live = Live(env_path=env_path, external=frozenset({"ANTHROPIC_API_KEY"}))
    app = create_app(store=BlobStore(tmp_path / "b"),
                     session_factory=sessionmaker(bind=engine), live=live)
    with TestClient(app) as client:
        sign_in(client, engine)
        assert "set by the server" in client.get("/setup").text
        page = client.post("/setup/model",
                           data={"api_key": "sk", "action": "save_anyway"},
                           headers=ORIGIN)
        assert "set by the server" in page.text
    assert "ANTHROPIC_API_KEY" not in envfile.read(env_path)


def test_signing_in_after_setup_lands_home(web, env_path, live):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk", "IMAP_HOST": "imap.gmail.com",
                             "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    live.reload()
    web.post("/logout", headers=ORIGIN)
    response = web.post("/login", data={"email": EMAIL, "password": PASSWORD},
                        headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_the_update_section_says_up_to_date(web, monkeypatch):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record(updates.Release((0, 0, 1), "v0.0.1", "p", None, None, None))
    monkeypatch.setattr(updates, "STATUS", status)
    assert "Up to date" in web.get("/setup").text


def test_the_update_section_says_when_it_could_not_check(web, monkeypatch):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record_error("no route to host")
    monkeypatch.setattr(updates, "STATUS", status)
    page = web.get("/setup")
    assert page.status_code == 200 and "Couldn" in page.text


def test_update_now_hands_off_on_windows(web, monkeypatch, tmp_path):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record(updates.Release((99, 0, 0), "v99.0.0", "p", "R-99.0.0-setup.exe", "e", "s"))
    monkeypatch.setattr(updates, "STATUS", status)
    monkeypatch.setattr(updates, "can_apply", lambda *a, **k: True)
    monkeypatch.setattr(updates, "download_verified", lambda r, d, **k: tmp_path / "x.exe")
    spawned = []
    monkeypatch.setattr(updates, "spawn_updater", lambda root, exe, **k: spawned.append(exe))
    assert "Update now" in web.get("/setup").text
    page = web.post("/setup/update", headers=ORIGIN)
    assert page.status_code == 200 and "Updating to 99.0.0" in page.text
    assert spawned == [tmp_path / "x.exe"]


def test_a_failed_download_is_said_on_the_page(web, monkeypatch):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record(updates.Release((99, 0, 0), "v99.0.0", "p", "R.exe", "e", "s"))
    monkeypatch.setattr(updates, "STATUS", status)
    monkeypatch.setattr(updates, "can_apply", lambda *a, **k: True)

    def fail(*a, **k):
        raise updates.UpdateError("the download does not match its checksum")

    monkeypatch.setattr(updates, "download_verified", fail)
    page = web.post("/setup/update", headers=ORIGIN)
    assert "does not match its checksum" in page.text


def test_update_now_elsewhere_offers_the_download_instead(web, monkeypatch):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record(updates.Release((99, 0, 0), "v99.0.0", "https://rel", "R.exe", "e", "s"))
    monkeypatch.setattr(updates, "STATUS", status)
    monkeypatch.setattr(updates, "can_apply", lambda *a, **k: False)
    page = web.get("/setup").text
    assert "https://rel" in page and "Update now" not in page
    assert web.post("/setup/update", headers=ORIGIN).status_code == 400


def test_other_pages_mention_an_available_update(web, monkeypatch):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record(updates.Release((99, 0, 0), "v99.0.0", "p", "R.exe", "e", "s"))
    monkeypatch.setattr(updates, "STATUS", status)
    assert "Renewal 99.0.0 is available" in web.get("/inbox").text


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def submit(self, fn, /, *args, **kwargs):
        self.calls.append(kwargs)


def test_an_upload_after_setup_uses_the_new_key_without_a_restart(
    engine, clean_db, live, env_path, tmp_path
):
    """Routes must read the client when they use it: one captured when the
    router was registered would still be the 'not set up' one."""
    from renewal.providers import is_configured

    runner = RecordingRunner()
    app = create_app(store=BlobStore(tmp_path / "blobs"),
                     session_factory=sessionmaker(bind=engine), live=live,
                     runner=runner)
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk-after-boot"})
    live.reload()
    with TestClient(app) as client:
        sign_in(client, engine)
        client.post("/documents", headers=ORIGIN,
                    files={"document": ("a.pdf", make_text_pdf([["Declarations"]]),
                                        "application/pdf")})
    assert runner.calls, "nothing was handed to the runner"
    assert is_configured(runner.calls[-1]["model_client"])
    assert runner.calls[-1]["settings"].anthropic_api_key == "sk-after-boot"
