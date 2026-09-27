"""Settings a running application can pick up without a restart."""

import os

import pytest

from renewal import envfile
from renewal.live import Live
from renewal.providers import is_configured


@pytest.fixture
def restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def env_path(tmp_path, monkeypatch, restore_environ):
    path = tmp_path / ".env"
    path.write_text("AGENCY_TZ=UTC\n")
    monkeypatch.setenv("RENEWAL_ENV_FILE", str(path))
    for key in envfile.OWNED:
        monkeypatch.delenv(key, raising=False)
    return path


def test_it_boots_with_no_key(env_path):
    live = Live(env_path=env_path, external=frozenset())
    assert not is_configured(live.model_client)


def test_a_saved_key_is_picked_up_on_reload(env_path):
    live = Live(env_path=env_path, external=frozenset())
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk-new"})
    live.reload()
    assert live.settings.anthropic_api_key == "sk-new"
    assert is_configured(live.model_client)


def test_a_cleared_value_is_cleared(env_path):
    envfile.write(env_path, {"IMAP_HOST": "imap.gmail.com"})
    live = Live(env_path=env_path, external=frozenset())
    assert live.settings.imap_host == "imap.gmail.com"
    envfile.write(env_path, {"IMAP_HOST": ""})
    live.reload()
    assert live.settings.imap_host == ""


def test_a_key_from_the_real_environment_wins_over_the_file(env_path, monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "from-systemd")
    live = Live(env_path=env_path, external=frozenset({"IMAP_HOST"}))
    envfile.write(env_path, {"IMAP_HOST": "from-page"})
    live.reload()
    assert live.settings.imap_host == "from-systemd"
    assert "IMAP_HOST" in live.external_keys


def test_from_process_snapshots_the_environment_before_reading_the_file(env_path, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "set-outside")
    live = Live.from_process(env_path)
    assert "SMTP_HOST" in live.external_keys
    assert "IMAP_HOST" not in live.external_keys
