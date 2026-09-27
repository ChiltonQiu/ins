"""Routes and clocks see a reload without a restart."""

import dataclasses
from pathlib import Path

from renewal.config import Settings
from renewal.digest.clock import DigestClock
from renewal.live import FixedLive
from renewal.mail.clock import MailClock
from renewal.web.deps import Deps


def _settings(**kw):
    base = Settings(
        database_url="postgresql+psycopg:///x", blob_root=Path("b"),
        anthropic_api_key="", extraction_model="m", draft_model="m",
        confidence_threshold=0.8, materiality_config=Path("m.yaml"),
    )
    return dataclasses.replace(base, **kw)


class Swappable:
    def __init__(self, settings, client=None):
        self.settings, self.model_client = settings, client


class NullSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass


def test_deps_read_through_to_live():
    live = Swappable(_settings(imap_host="a"))
    deps = Deps(live=live, store=None, session_factory=None)
    live.settings = _settings(imap_host="b")
    assert deps.settings.imap_host == "b"


def test_the_mail_clock_reads_live_on_every_tick(monkeypatch):
    seen = []

    def fake_poll(session, store, *, settings, client, on_document, opener):
        seen.append((settings.imap_host, client))

    monkeypatch.setattr("renewal.mail.clock.poll_once", fake_poll)
    live = Swappable(_settings(imap_host=""), client="c1")
    clock = MailClock(lambda: NullSession(), None, live.settings, live=live)
    clock.tick()
    live.settings, live.model_client = _settings(imap_host="imap.gmail.com"), "c2"
    clock.tick()
    assert seen == [("", "c1"), ("imap.gmail.com", "c2")]


def test_the_digest_clock_reads_live_on_every_tick(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "renewal.digest.clock.effective", lambda session, settings: settings
    )
    monkeypatch.setattr(
        "renewal.digest.clock.send_due_digest",
        lambda session, settings, send: seen.append(settings.smtp_host),
    )
    live = Swappable(_settings(smtp_host=""))
    clock = DigestClock(lambda: NullSession(), live.settings, live=live)
    clock.tick()
    live.settings = _settings(smtp_host="smtp.gmail.com")
    clock.tick()
    assert seen == ["", "smtp.gmail.com"]


def test_fixed_live_keeps_the_old_create_app_signature_working():
    live = FixedLive(_settings(), None)
    assert live.external_keys == frozenset()
