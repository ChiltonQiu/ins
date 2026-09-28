# Setup Page and One-Click Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take a fresh install from "installed" to "reading carrier mail" from a page in the application, and let a Windows install update itself from a button; ship both as 0.3.0.

**Architecture:** A `Live` object owns the current `Settings` and model client and can reload them from `.env`, which `renewal/envfile.py` edits in place. Routes and clocks read from `Live` instead of holding startup values. `/setup` tests proposed values through `renewal/setup/checks.py` before writing them. Updates are checked by a background clock against GitHub releases; applying one downloads and verifies the `.exe`, then hands off to a detached `scripts/update.ps1` that backs up, stops, installs and restarts.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLAlchemy, python-dotenv, httpx, anthropic SDK, imaplib/smtplib, PowerShell 5.1, NSIS, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-27-setup-page-design.md` and `docs/superpowers/specs/2026-09-27-one-click-update-design.md`

## Global Constraints

- Every file Windows runs (`scripts/*.ps1`, `*.cmd`, `packaging/*.nsi`) stays ASCII (`tests/test_windows_scripts.py`).
- Native commands in `.ps1` files that redirect stderr go through `Invoke-Native`; `pg_ctl start` is never piped.
- Credentials are typed into `/setup`, written only to `.env`, never stored in the database, never rendered into any response.
- `.env` is written UTF-8 with no BOM.
- The mailbox stays read-only: every IMAP access goes through `renewal/mail/imapbox.py`'s `Mailbox` (EXAMINE).
- Anything a page shows about a failure is one sentence and one next step; unrecognised errors show "Something went wrong: " plus the real error.
- Tests: `.venv/bin/pytest` (needs PostgreSQL). No test touches the real Anthropic API, Gmail or GitHub.
- Commit messages end with the attribution lines from the session's system reminder.

## Review Focus

1. **A password containing `#`, quotes, `=`, spaces or a trailing backslash** — must round-trip through `.env` unchanged, or mail silently stops logging in. Pinned in Task 2.
2. **A key set in the real environment (systemd, CI)** — the page must say so and not write a value that silently does nothing. Pinned in Task 4 and Task 8.
3. **Listing folders, then choosing one** — the password must not have to be typed twice, and polling must not start on INBOX in between. Pinned in Task 8.
4. **The app's own process tree on Windows** — the venv launcher runs the interpreter as a child in a job object; an updater spawned from the app must survive the app being stopped. Pinned in Task 10 (breakaway flag) and exercised end to end in CI (Task 11).
5. **An update check with no network, or a malformed release** — must show "couldn't check", never break `/setup`. Pinned in Task 9.

---

### Task 1: One `.env` path, and the update switches

**Files:**
- Modify: `renewal/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `env_file_path() -> Path`; `Settings.update_check: bool = True`; `Settings.update_api_url: str = "https://api.github.com/repos/ChiltonQiu/ins/releases/latest"`; `load_settings()` reads `.env` from `env_file_path()`.

- [x] **Step 1: Write the failing tests** (append to `tests/test_config.py`)

```python
def test_the_env_file_is_the_one_beside_the_package(monkeypatch):
    monkeypatch.delenv("RENEWAL_ENV_FILE", raising=False)
    root = Path(config.__file__).resolve().parent.parent
    assert config.env_file_path() == root / ".env"


def test_the_env_file_can_be_pointed_elsewhere(monkeypatch, tmp_path):
    monkeypatch.setenv("RENEWAL_ENV_FILE", str(tmp_path / "x.env"))
    assert config.env_file_path() == tmp_path / "x.env"


def test_settings_are_read_from_that_file(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("AGENCY_TZ=America/Chicago\n")
    monkeypatch.setenv("RENEWAL_ENV_FILE", str(env))
    monkeypatch.delenv("AGENCY_TZ", raising=False)
    assert config.load_settings().agency_tz == "America/Chicago"


def test_the_update_check_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("UPDATE_CHECK", "false")
    assert config.load_settings().update_check is False


def test_the_update_check_is_on_by_default(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("UPDATE_CHECK", raising=False)
    monkeypatch.delenv("UPDATE_API_URL", raising=False)
    settings = config.load_settings()
    assert settings.update_check is True
    assert settings.update_api_url.endswith("/repos/ChiltonQiu/ins/releases/latest")
```

(Add `from pathlib import Path` to the imports if absent.)

- [x] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_config.py -q` → FAIL (`env_file_path` missing).

- [x] **Step 3: Implement** in `renewal/config.py`:

```python
def env_file_path() -> Path:
    """The .env this installation reads and /setup writes.

    One path, named, rather than load_dotenv()'s search up from the calling
    file: the page that edits the file and the loader that reads it must never
    be able to disagree about which file that is. RENEWAL_ENV_FILE is for
    tests and for a deployment that keeps it elsewhere.
    """
    override = os.environ.get("RENEWAL_ENV_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".env"
```

Add to `Settings` (after `usage_tracking`):

```python
    # Asking GitHub once every twelve hours whether a newer release exists.
    # The one thing this application does that tells a third party the
    # office's address, so it has an off switch.
    update_check: bool = True
    update_api_url: str = (
        "https://api.github.com/repos/ChiltonQiu/ins/releases/latest"
    )
```

In `load_settings`: replace `load_dotenv()` with `load_dotenv(env_file_path())`, and add:

```python
        update_check=os.environ.get(
            "UPDATE_CHECK", "true"
        ).lower() not in ("0", "false", "no"),
        update_api_url=os.environ.get(
            "UPDATE_API_URL",
            "https://api.github.com/repos/ChiltonQiu/ins/releases/latest",
        ),
```

Add to `.env.example`, after the usage-tracking block:

```
# Once every twelve hours the application asks GitHub whether a newer release
# exists, and /setup offers it. That request is the only thing this
# application sends anywhere unprompted. false turns it off.
UPDATE_CHECK=true
```

- [x] **Step 4: Run** `.venv/bin/pytest tests/test_config.py -q` → PASS.
- [x] **Step 5: Commit** — `feat(config): one named .env path, and a switch for the update check`

---

### Task 2: Editing `.env` in place

**Files:**
- Create: `renewal/envfile.py`
- Test: `tests/test_envfile.py`

**Interfaces:**
- Produces: `OWNED: frozenset[str]`; `read(path: Path) -> dict[str, str]`; `write(path: Path, updates: Mapping[str, str]) -> None`; `quote(value: str) -> str`.

- [x] **Step 1: Write the failing tests** — `tests/test_envfile.py`:

```python
"""Editing .env without disturbing anything the page does not own."""

import pytest
from dotenv import dotenv_values

from renewal import envfile

HOSTILE = [
    "plain",
    "has#hash",
    "has 'single' quotes",
    'has "double" quotes',
    "has=equals",
    "  spaces  around  ",
    "back\\slash",
    "trailing\\",
    "\\\\double-backslash",
    "café-ünicode",
    "$HOME-not-expanded",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_value_round_trips(tmp_path, value):
    path = tmp_path / ".env"
    envfile.write(path, {"IMAP_PASSWORD": value})
    assert dotenv_values(path)["IMAP_PASSWORD"] == value
    assert envfile.read(path)["IMAP_PASSWORD"] == value


def test_lines_it_does_not_own_are_left_byte_for_byte(tmp_path):
    path = tmp_path / ".env"
    before = (
        "# a comment\r\n"
        "DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5433/renewal\r\n"
        "\r\n"
        "IMAP_HOST=old.example.com\r\n"
        "BLOB_ENCRYPTION_KEY=abc=\r\n"
    )
    path.write_bytes(before.encode())
    envfile.write(path, {"IMAP_HOST": "imap.gmail.com"})
    after = path.read_bytes().decode()
    assert after == before.replace(
        "IMAP_HOST=old.example.com", 'IMAP_HOST="imap.gmail.com"'
    )


def test_a_missing_key_is_appended(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    envfile.write(path, {"IMAP_USER": "her@gmail.com"})
    assert path.read_text() == 'A=1\nIMAP_USER="her@gmail.com"\n'


def test_a_missing_file_is_created(tmp_path):
    path = tmp_path / ".env"
    envfile.write(path, {"IMAP_USER": "x"})
    assert envfile.read(path) == {"IMAP_USER": "x"}


def test_a_later_duplicate_is_removed_so_it_cannot_win(tmp_path):
    path = tmp_path / ".env"
    path.write_text("IMAP_HOST=a\nOTHER=1\nIMAP_HOST=b\n")
    envfile.write(path, {"IMAP_HOST": "c"})
    assert path.read_text() == 'IMAP_HOST="c"\nOTHER=1\n'


def test_no_byte_order_mark_is_written_and_one_is_dropped(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes("﻿A=1\n".encode("utf-8"))
    envfile.write(path, {"IMAP_USER": "x"})
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert envfile.read(path)["A"] == "1"


def test_a_value_with_a_newline_is_refused(tmp_path):
    with pytest.raises(ValueError):
        envfile.write(tmp_path / ".env", {"IMAP_PASSWORD": "a\nb"})


def test_a_failed_write_leaves_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=1\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(envfile.os, "replace", boom)
    with pytest.raises(OSError):
        envfile.write(path, {"A": "2"})
    assert path.read_text() == "A=1\n"


def test_reading_a_missing_file_is_empty(tmp_path):
    assert envfile.read(tmp_path / "nope.env") == {}
```

- [x] **Step 2: Run** `.venv/bin/pytest tests/test_envfile.py -q` → FAIL (no module).

- [x] **Step 3: Implement** `renewal/envfile.py`:

```python
"""Changing .env from the setup page, and nothing else in it.

The file is also hand-edited, and the installer writes to it, so this touches
only the lines for the keys it is given: every comment, blank and unrelated
setting comes back out byte for byte. A value is always written double-quoted
with backslash and quote escaped, which is the one form python-dotenv reads
back exactly whatever a password contains.

UTF-8 with no byte-order mark: python-dotenv reads a BOM as part of the first
key's name, and that setting silently stops existing (scripts/install.ps1
learned this first).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

# The keys /setup writes. Everything else in the file is the installer's or
# the operator's.
OWNED = frozenset({
    "PROVIDER", "ANTHROPIC_API_KEY",
    "IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD", "IMAP_FOLDER",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFY_FROM",
})

_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("a setting cannot contain a line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path, encoding="utf-8-sig").items()}


def write(path: Path, updates: Mapping[str, str]) -> None:
    rendered = {key: f"{key}={quote(value)}" for key, value in updates.items()}
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    out: list[str] = []
    done: set[str] = set()
    for line in lines:
        match = _KEY.match(line)
        key = match.group(1) if match else None
        if key in rendered:
            if key in done:
                continue  # a later duplicate would win over the edit
            ending = line[len(line.rstrip("\r\n")):] or newline
            out.append(rendered[key] + ending)
            done.add(key)
        else:
            out.append(line)

    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += newline
    for key, line in rendered.items():
        if key not in done:
            out.append(line + newline)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes("".join(out).encode("utf-8"))
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
```

- [x] **Step 4: Run** `.venv/bin/pytest tests/test_envfile.py -q` → PASS.
- [x] **Step 5: Commit** — `feat: edit .env in place, one key at a time`

---

### Task 3: Booting without a key

**Files:**
- Modify: `renewal/providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces: `ModelNotConfigured(RuntimeError)`; `UnconfiguredClient(reason: str)` with `.configured = False`, `.reason`, `.complete(**kw)` raising `ModelNotConfigured`; `build_client_or_unconfigured(settings) -> ModelClient`; `is_configured(client) -> bool`.

- [x] **Step 1: Write the failing tests** (append to `tests/test_providers.py`; reuse its existing settings helper if there is one, else build `Settings` as below):

```python
def _bare_settings(**overrides):
    from pathlib import Path
    from renewal.config import Settings
    base = Settings(
        database_url="postgresql+psycopg:///x", blob_root=Path("b"),
        anthropic_api_key="", extraction_model="m", draft_model="m",
        confidence_threshold=0.8, materiality_config=Path("m.yaml"),
    )
    import dataclasses
    return dataclasses.replace(base, **overrides)


def test_no_key_gives_a_client_that_says_so():
    from renewal.providers import (
        ModelNotConfigured, build_client_or_unconfigured, is_configured,
    )
    client = build_client_or_unconfigured(_bare_settings())
    assert not is_configured(client)
    with pytest.raises(ModelNotConfigured, match="not set up"):
        client.complete(model="m", system="s", content=[])


def test_a_key_gives_a_real_client():
    from renewal.providers import build_client_or_unconfigured, is_configured
    client = build_client_or_unconfigured(_bare_settings(anthropic_api_key="k"))
    assert is_configured(client)
```

- [x] **Step 2: Run** `.venv/bin/pytest tests/test_providers.py -q` → FAIL (names missing).

- [x] **Step 3: Implement** — append to `renewal/providers.py`:

```python
class ModelNotConfigured(RuntimeError):
    """A model stage ran before anybody entered a key."""


class UnconfiguredClient:
    """What the application runs on until /setup has a key.

    build_client raises on a missing key so that a script fails at once; the
    web application must instead come up, because the page that fixes the key
    is served by it. Every stage that calls this fails the way a stage fails
    today -- logged and skipped -- so a document that arrives first is stored,
    read and searchable, and its model stages wait for a Retry.
    """

    configured = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def complete(self, **kwargs) -> str:
        raise ModelNotConfigured(self.reason)


def build_client_or_unconfigured(settings: Settings) -> ModelClient:
    try:
        return build_client(settings)
    except ValueError as exc:
        return UnconfiguredClient(f"AI is not set up yet ({exc})")


def is_configured(client) -> bool:
    return getattr(client, "configured", True) and client is not None
```

- [x] **Step 4: Run** → PASS.
- [x] **Step 5: Commit** — `feat(providers): a client for when there is no key yet`

---

### Task 4: `Live` — settings that can change under a running app

**Files:**
- Create: `renewal/live.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Consumes: `env_file_path`, `load_settings` (Task 1); `envfile.read`, `envfile.OWNED` (Task 2); `build_client_or_unconfigured` (Task 3).
- Produces: `class Live` with `settings: Settings`, `model_client`, `env_path: Path`, `external_keys: frozenset[str]`, `reload() -> None`, classmethod `Live.from_process(env_path: Path | None = None) -> Live`; `class FixedLive(settings, model_client)` with the same read interface, `env_path = None`, `external_keys = frozenset()`, and `reload()` raising `RuntimeError`.

- [x] **Step 1: Write the failing tests** — `tests/test_live.py`:

```python
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
```

- [x] **Step 2: Run** `.venv/bin/pytest tests/test_live.py -q` → FAIL.

- [x] **Step 3: Implement** `renewal/live.py`:

```python
"""The settings and model client in force right now.

Everything else in the application was built on a frozen Settings read once
at import. That stays true of everything the page does not change; for what
it does -- the key, the mailbox, the sender -- routes and clocks ask this
object each time instead, and /setup calls reload() after writing .env.

Which of those keys came from the real environment is decided once, before
.env is first read: a key systemd or a shell set wins over the file (that is
load_dotenv's rule), so the page has to know not to offer to change it.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from renewal import envfile
from renewal.config import Settings, env_file_path, load_settings
from renewal.providers import build_client_or_unconfigured


class Live:
    def __init__(
        self, *, env_path: Path, external: frozenset[str],
        load=load_settings, build=build_client_or_unconfigured,
    ) -> None:
        self._env_path = env_path
        self._external = external
        self._load = load
        self._build = build
        self._lock = threading.Lock()
        self._settings: Settings | None = None
        self._model_client = None
        self.reload()

    @classmethod
    def from_process(cls, env_path: Path | None = None) -> "Live":
        external = frozenset(os.environ)
        return cls(env_path=env_path or env_file_path(), external=external)

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def model_client(self):
        return self._model_client

    @property
    def env_path(self) -> Path:
        return self._env_path

    @property
    def external_keys(self) -> frozenset[str]:
        return self._external & envfile.OWNED

    def reload(self) -> None:
        with self._lock:
            values = envfile.read(self._env_path)
            for key, value in values.items():
                if key not in self._external:
                    os.environ[key] = value
            for key in envfile.OWNED - values.keys() - self._external:
                os.environ.pop(key, None)
            settings = self._load()
            client = self._build(settings)
            self._settings, self._model_client = settings, client


class FixedLive:
    """For an application built with a Settings and a client in hand -- every
    existing test, and the scripts. Nothing to reload."""

    env_path = None
    external_keys = frozenset()

    def __init__(self, settings: Settings, model_client) -> None:
        self.settings = settings
        self.model_client = model_client

    def reload(self) -> None:
        raise RuntimeError("this application was built with fixed settings")
```

- [x] **Step 4: Run** → PASS.
- [x] **Step 5: Commit** — `feat: Live, the settings a running application can reload`

---

### Task 5: Routes and clocks read `Live`; the app boots without a key

**Files:**
- Modify: `renewal/web/deps.py`, `renewal/web/__init__.py`, `renewal/app.py`, `renewal/mail/clock.py`, `renewal/digest/clock.py`
- Test: `tests/test_live_wiring.py`

**Interfaces:**
- Consumes: `Live`, `FixedLive` (Task 4).
- Produces: `Deps(live, store, session_factory, runner)` with properties `settings`, `model_client`; `create_app(*, store, session_factory, settings=None, model_client=None, live=None, inbound_provider=None, runner=None)`; `MailClock(..., live=None)` and `DigestClock(..., live=None)` read `live` each tick when given.

- [x] **Step 1: Write the failing tests** — `tests/test_live_wiring.py`:

```python
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

    class NullSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass

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

    class NullSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass

    live = Swappable(_settings(smtp_host=""))
    clock = DigestClock(lambda: NullSession(), live.settings, live=live)
    clock.tick()
    live.settings = _settings(smtp_host="smtp.gmail.com")
    clock.tick()
    assert seen == ["", "smtp.gmail.com"]


def test_fixed_live_keeps_the_old_create_app_signature_working():
    live = FixedLive(_settings(), None)
    assert live.external_keys == frozenset()
```

- [x] **Step 2: Run** `.venv/bin/pytest tests/test_live_wiring.py -q` → FAIL.

- [x] **Step 3: Implement.**

`renewal/web/deps.py` — replace the dataclass:

```python
@dataclass(frozen=True)
class Deps:
    # Settings and the model client are read through Live on every access,
    # because /setup can change them under a running application.
    live: Any
    store: BlobStore
    session_factory: Any
    # Where work that must not block the response goes. The application builds
    # a ThreadRunner; tests pass an InlineRunner so their assertions are not
    # racing a thread.
    runner: Any = None

    @property
    def settings(self) -> Settings:
        return self.live.settings

    @property
    def model_client(self) -> Any:
        return self.live.model_client
```

`renewal/web/__init__.py` — new signature and construction:

```python
def create_app(
    *, store: BlobStore, session_factory, settings: Settings | None = None,
    model_client=None, live=None, inbound_provider=None, runner=None,
):
    if live is None:
        from renewal.live import FixedLive
        live = FixedLive(settings, model_client)
    settings = live.settings
```

and build `Deps(live=live, store=store, session_factory=session_factory, runner=...)`.

`renewal/mail/clock.py` — add `live=None` keyword to `__init__` (store `self._live = live`), and at the top of `tick()`:

```python
        settings = self._live.settings if self._live else self._settings
        client = self._live.model_client if self._live else self._client
```

then pass `settings=settings, client=client` to `poll_once`, and make `_hand_off` use `self._live.settings`/`.model_client` when `self._live` is set.

`renewal/digest/clock.py` — add `live=None`, and in `tick()` use `effective(session, self._live.settings if self._live else self._settings)`.

`renewal/app.py` — the composition root:

```python
from renewal.live import Live

# First, before anything reads .env: which keys the real environment set is
# decided from what is in os.environ now.
_live = Live.from_process()
_settings = _live.settings
_session_factory = sessionmaker(bind=get_engine())
_store = store_from_settings(_settings)
_runner = ThreadRunner(_settings.intake_workers)

app = create_app(
    store=_store, session_factory=_session_factory, runner=_runner, live=_live,
)

if _settings.digest_tick_seconds > 0:
    DigestClock(
        _session_factory, _settings,
        tick_seconds=_settings.digest_tick_seconds, live=_live,
    ).start()

# Always started: /setup can turn mail on after boot, and a tick with no host
# returns at once (poll_once's first line).
if _settings.imap_poll_seconds > 0:
    MailClock(
        _session_factory, _store, _settings,
        tick_seconds=_settings.imap_poll_seconds,
        runner=_runner, live=_live,
    ).start()
```

Update the module docstring's clock paragraph to say the mail clock always starts.

- [x] **Step 4: Run** `.venv/bin/pytest tests/test_live_wiring.py -q` → PASS, then the full suite `.venv/bin/pytest -q -x` → PASS (every existing `create_app(settings=..., model_client=...)` call keeps working through `FixedLive`).
- [x] **Step 5: Commit** — `feat: routes and clocks read settings through Live`

---

### Task 6: Folders, and starting from now

**Files:**
- Modify: `renewal/mail/imapbox.py`, `renewal/mail/poll.py`
- Test: `tests/test_imapbox.py`, `tests/test_mail_poll.py`

**Interfaces:**
- Produces: `Mailbox.list_folders() -> list[str]` (selectable folders, decoded); `poll.start_from_now(session, settings, *, opener=open_mailbox) -> int` (sets `last_uid` to the folder's highest UID, returns message count); `poll.start_from_beginning(session, settings) -> None` (clears `last_uid` and `uid_validity`).

- [x] **Step 1: Write the failing tests.** In `tests/test_imapbox.py` (reuse its fake IMAP object; if it has none, this one):

```python
class ListingImap:
    def __init__(self, lines):
        self._lines = lines
    def login(self, user, password): return ("OK", [b"ok"])
    def examine(self, folder): return ("OK", [b"1"])
    def list(self): return ("OK", self._lines)
    def logout(self): return ("BYE", [])


def test_folders_are_listed_decoded_and_unselectable_ones_dropped():
    from renewal.mail.imapbox import Mailbox
    lines = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren) "/" "Carriers"',
        b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
        b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
        b'(\\HasNoChildren) "/" "Quote \\"Two\\""',
        b'(\\HasNoChildren) "/" Plain',
    ]
    box = Mailbox("h", "u", "p", connector=lambda h, p: ListingImap(lines))
    with box:
        assert box.list_folders() == [
            "INBOX", "Carriers", "[Gmail]/All Mail", 'Quote "Two"', "Plain",
        ]
```

In `tests/test_mail_poll.py`:

```python
def test_start_from_now_skips_what_is_already_there(session, settings_for_poll):
    from renewal.mail.poll import _state, start_from_now

    class Box:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def uid_validity(self): return 7
        def uids_since(self, uid): return [3, 9, 41]

    count = start_from_now(session, settings_for_poll, opener=lambda s: Box())
    state = _state(session, settings_for_poll)
    assert (count, state.uid_validity, state.last_uid) == (3, 7, 41)


def test_start_from_now_on_an_empty_folder_starts_at_zero(session, settings_for_poll):
    from renewal.mail.poll import _state, start_from_now

    class Box:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def uid_validity(self): return 7
        def uids_since(self, uid): return []

    assert start_from_now(session, settings_for_poll, opener=lambda s: Box()) == 0
    assert _state(session, settings_for_poll).last_uid == 0


def test_start_from_the_beginning_forgets_the_mark(session, settings_for_poll):
    from renewal.mail.poll import _state, start_from_beginning
    state = _state(session, settings_for_poll)
    state.uid_validity, state.last_uid = 7, 41
    start_from_beginning(session, settings_for_poll)
    assert (state.uid_validity, state.last_uid) == (None, None)
```

(`settings_for_poll`: use the settings fixture `tests/test_mail_poll.py` already builds, with `imap_host="imap.gmail.com"`, `imap_folder="Carriers"`; add one if it has none.)

- [x] **Step 2: Run** `.venv/bin/pytest tests/test_imapbox.py tests/test_mail_poll.py -q` → FAIL.

- [x] **Step 3: Implement.** In `imapbox.py`:

```python
_LIST = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def _unquote(raw: bytes) -> str:
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = re.sub(rb'\\(.)', rb'\1', raw[1:-1])
    return raw.decode("utf-8", errors="replace")
```

and on `Mailbox`:

```python
    def list_folders(self) -> list[str]:
        """Every folder that can be opened, by the name EXAMINE takes. A
        \\Noselect entry is a label's parent, not a folder, and choosing it
        would fail on the first poll."""
        data = self._check(self._imap.list(), "list")
        names = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            found = _LIST.match(line)
            if found is None or b"\\noselect" in found.group("flags").lower():
                continue
            names.append(_unquote(found.group("name")))
        return names
```

In `poll.py`:

```python
def start_from_now(session: Session, settings: Settings, *, opener=open_mailbox) -> int:
    """Mark everything already in the folder as read, so the first poll takes
    only what arrives from here on. A folder of years of carrier mail read in
    full is a bill and an afternoon nobody asked for."""
    with opener(settings) as box:
        validity = box.uid_validity()
        uids = box.uids_since(None)
    state = _state(session, settings)
    state.uid_validity = validity
    state.last_uid = max(uids) if uids else 0
    session.flush()
    return len(uids)


def start_from_beginning(session: Session, settings: Settings) -> None:
    state = _state(session, settings)
    state.uid_validity = None
    state.last_uid = None
    session.flush()
```

- [x] **Step 4: Run** → PASS.
- [x] **Step 5: Commit** — `feat(mail): list folders, and start a folder from now`

---

### Task 7: The checks behind the buttons

**Files:**
- Create: `renewal/setup/__init__.py` (empty docstring), `renewal/setup/checks.py`, `renewal/setup/status.py`
- Modify: `pyproject.toml` (add `"renewal.setup"` to `[tool.setuptools] packages`)
- Test: `tests/test_setup_checks.py`

**Interfaces:**
- Consumes: `Mailbox` (+ `list_folders`), `send_email`, `build_client`, `text_block`, `is_configured`.
- Produces:
  - `CheckResult(ok: bool, message: str, detail: str = "", warning: bool = False)`
  - `check_model(settings, key, *, build=build_client) -> CheckResult`
  - `list_folders(host, port, user, password, *, connector=None) -> tuple[CheckResult, list[str]]`
  - `check_mailbox(host, port, user, password, folder, *, connector=None) -> tuple[CheckResult, int]`
  - `check_smtp(settings, *, host, port, user, password, sender, to, send=send_email) -> CheckResult`
  - `smtp_host_for(imap_host) -> str | None`
  - `scrub(text, secrets) -> str`
  - `SetupStatus(model: bool, mail: bool, summary: bool)` with `.done` (0–2) and `.complete`; `status_of(settings, model_client) -> SetupStatus`

- [x] **Step 1: Write the failing tests** — `tests/test_setup_checks.py`:

```python
"""Each failure somebody will actually hit, as the sentence they will read."""

import dataclasses
import imaplib
import smtplib
import socket
from pathlib import Path

import anthropic
import httpx
import pytest

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


class FakeImap:
    def __init__(self, *, login_error=None, folders=(), uids=b""):
        self.login_error, self.folders, self.uids = login_error, folders, uids
    def login(self, user, password):
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return ("OK", [b"ok"])
    def examine(self, folder): return ("OK", [b"1"])
    def list(self): return ("OK", list(self.folders))
    def uid(self, command, *args): return ("OK", [self.uids])
    def logout(self): return ("BYE", [])


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
    fake = FakeImap(folders=[b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Carriers"'])
    result, folders = checks.list_folders("imap.gmail.com", 993, "u", "p", connector=lambda h, p: fake)
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
```

- [x] **Step 2: Run** `.venv/bin/pytest tests/test_setup_checks.py -q` → FAIL.

- [x] **Step 3: Implement.** `renewal/setup/__init__.py`:

```python
"""Getting from installed to working, from a page."""
```

`renewal/setup/status.py`:

```python
"""Whether setup is finished, worked out from the settings in force.

Computed on every request rather than stored: a "setup complete" flag is one
more thing that can disagree with the file it describes.
"""

from __future__ import annotations

from dataclasses import dataclass

from renewal.config import Settings
from renewal.providers import is_configured


@dataclass(frozen=True)
class SetupStatus:
    model: bool
    mail: bool
    summary: bool

    @property
    def done(self) -> int:
        return int(self.model) + int(self.mail)

    @property
    def complete(self) -> bool:
        return self.model and self.mail


def status_of(settings: Settings, model_client) -> SetupStatus:
    return SetupStatus(
        model=is_configured(model_client),
        mail=bool(settings.imap_host and settings.imap_user and settings.imap_password),
        summary=bool(settings.smtp_host and settings.notify_to),
    )
```

`renewal/setup/checks.py`:

```python
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


def _unexpected(exc: Exception) -> str:
    return f"Something went wrong: {exc}"


# ------------------------------------------------------------------- model

def model_message(exc: Exception) -> str:
    import anthropic

    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ("That key didn't work. Copy it again from console.anthropic.com "
                "→ API keys, and paste the whole thing.")
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic. Is this computer online?"
    text = str(exc).lower()
    if "credit balance" in text or "billing" in text:
        return ("The key works, but the account has no credit. Add some at "
                "console.anthropic.com → Billing.")
    return _unexpected(exc)


def check_model(settings: Settings, key: str, *, build=build_client) -> CheckResult:
    proposed = dataclasses.replace(settings, provider="anthropic", anthropic_api_key=key)
    try:
        build(proposed).complete(
            model=proposed.classification_model,
            system="Reply with the single word OK.",
            content=[text_block("Are you there?")],
        )
    except Exception as exc:  # noqa: BLE001 - every failure becomes a sentence
        return CheckResult(False, model_message(exc), scrub(repr(exc), [key]))
    return CheckResult(True, "Connected to Anthropic.")


# -------------------------------------------------------------------- mail

def mail_message(exc: Exception, host: str) -> str:
    text = str(exc).lower()
    gmail = host.lower() == GMAIL_IMAP
    if "not enabled for imap" in text or "imap access is disabled" in text:
        return ("IMAP is turned off for this account. In Gmail: Settings → See all "
                "settings → Forwarding and POP/IMAP → Enable IMAP.")
    if ("application-specific password" in text or "authenticationfailed" in text
            or "invalid credentials" in text or "authentication failed" in text
            or "login failed" in text):
        if gmail:
            return ("Gmail refused the login. It needs an app password, not the "
                    f"normal one — make one at {APP_PASSWORD_URL}")
        return "The mail server refused the login. Check the address and password."
    if isinstance(exc, OSError):
        return f"Couldn't reach {host}. Is this computer online?"
    return _unexpected(exc)


def list_folders(host, port, user, password, *, connector=None):
    box = Mailbox(host, user, password, port=port, connector=connector or _connect)
    try:
        with box:
            folders = box.list_folders()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, mail_message(exc, host), scrub(repr(exc), [password])), []
    return CheckResult(True, f"Signed in. Found {len(folders)} folders."), folders


def check_mailbox(host, port, user, password, folder, *, connector=None):
    box = Mailbox(host, user, password, folder=folder, port=port,
                  connector=connector or _connect)
    try:
        with box:
            count = len(box.uids_since(None))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, mail_message(exc, host), scrub(repr(exc), [password])), 0
    if count == 0:
        return CheckResult(True, f"Connected. {folder} is empty — is the Gmail "
                                 "filter that files carrier mail there set up?",
                           warning=True), 0
    return CheckResult(True, f"Connected. {count} messages in {folder}."), count


# ------------------------------------------------------------------- sending

def smtp_host_for(imap_host: str) -> str | None:
    return {GMAIL_IMAP: "smtp.gmail.com"}.get(imap_host.lower())


def check_smtp(settings, *, host, port, user, password, sender, to, send=send_email):
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
        return CheckResult(False, f"{who} refused to send. Is it the same app "
                                  "password as above?", scrub(repr(exc), [password]))
    except smtplib.SMTPException as exc:
        return CheckResult(False, f"The mail server refused: {exc}",
                           scrub(repr(exc), [password]))
    except OSError as exc:
        return CheckResult(False, f"Couldn't reach {host}. Is this computer online?",
                           scrub(repr(exc), [password]))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, _unexpected(exc), scrub(repr(exc), [password]))
    return CheckResult(True, f"Sent a test message to {to}.")
```

Add `"renewal.setup",` to the `packages` list in `pyproject.toml`.

- [x] **Step 4: Run** `.venv/bin/pytest tests/test_setup_checks.py tests/test_packaging.py -q` → PASS.
- [x] **Step 5: Commit** — `feat(setup): the checks behind the buttons, as sentences`

---

### Task 8: `/setup`, the banner, and the first screen

**Files:**
- Create: `renewal/web/setup.py`, `renewal/templates/setup.html`
- Modify: `renewal/web/__init__.py` (register), `renewal/web/navbadge.py` (setup status into `request.state`), `renewal/web/templating.py` (expose it), `renewal/templates/base.html` (nav link + banner), `renewal/static/app.css` (card styles)
- Test: `tests/test_web_setup.py`

**Interfaces:**
- Consumes: `Live` (Task 4), `envfile.write` (Task 2), `checks.*`, `status_of` (Task 7), `start_from_now`/`start_from_beginning` (Task 6), `settings_store.write`.
- Produces: `GET /setup`; `POST /setup/model` (`api_key`, `action`); `POST /setup/mail/folders` (`email`, `password`, `host`, `port`); `POST /setup/mail` (`folder`, `start` = `new`|`all`, `host`, `port`, `action`); `POST /setup/summary` (`to`, `same_account`, `host`, `port`, `user`, `password`, `action`). `action` is `test` or `save_anyway`. `request.state.setup_status: SetupStatus`.

Behaviour, stated exactly:
- Every POST refuses a key in `live.external_keys` with "This is set by the server's environment — change it there." and writes nothing for it.
- A blank secret field means "keep the saved value"; a blank with nothing saved is refused ("Paste the key first." / "Type the app password first.").
- `POST /setup/mail/folders` on success writes `IMAP_USER`, `IMAP_PASSWORD` and **`IMAP_HOST=""`** (polling stays off until a folder is chosen), reloads, and renders the page with a folder `<select>` and hidden `host`/`port` fields. The password is never re-rendered.
- `POST /setup/mail` requires the model to be configured ("Set up the AI first — mail read without it is stored but not read."). On a passing check (or `save_anyway`) it runs `start_from_now` (default) or `start_from_beginning` on proposed settings, commits, then writes `IMAP_HOST`, `IMAP_PORT`, `IMAP_FOLDER` and reloads.
- `POST /setup/summary` with blank `to` turns sending off (`SMTP_HOST=""`, `notify_to` stored empty). With `same_account` it uses `smtp_host_for(imap_host)` (refusing "Not Gmail? Untick 'same account' and fill in the server." when `None`), port 587, the saved IMAP user and password, sender = IMAP user.
- Success redirects `303` to `/setup?saved=<card>#<card>`; a failed check renders the page (200) with the message and a **Save anyway** button.
- Banner: on every page but `/setup` itself, while `not setup_status.complete`: "Finish setup ({done} of 2 done) →" linking `/setup`.
- After sign-in with a default `next` of `/`, a session whose setup is incomplete lands on `/setup` (`renewal/web/auth.py` login redirect).

- [x] **Step 1: Write the failing tests** — `tests/test_web_setup.py`:

```python
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
from tests.authhelp import sign_in

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
    page = web.post("/setup/model", data={"api_key": "sk-bad", "action": "test"}, headers=ORIGIN)
    assert "That key didn't work." in page.text and "Save anyway" in page.text
    assert "ANTHROPIC_API_KEY" not in envfile.read(env_path)


def test_save_anyway_saves_without_checking(web, env_path, monkeypatch):
    def never(*a, **k):
        raise AssertionError("checked")
    monkeypatch.setattr("renewal.setup.checks.check_model", never)
    web.post("/setup/model", data={"api_key": "sk-x", "action": "save_anyway"}, headers=ORIGIN)
    assert envfile.read(env_path)["ANTHROPIC_API_KEY"] == "sk-x"


def test_a_blank_key_keeps_the_saved_one(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk-kept"})
    live.reload()
    seen = []
    monkeypatch.setattr("renewal.setup.checks.check_model",
                        lambda s, k, **kw: seen.append(k) or ok())
    web.post("/setup/model", data={"api_key": "", "action": "test"}, headers=ORIGIN)
    assert seen == ["sk-kept"]


def test_no_secret_ever_reaches_a_page(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk-SECRET-1",
                             "IMAP_PASSWORD": "pw-SECRET-2", "IMAP_USER": "her@gmail.com"})
    live.reload()
    page = web.get("/setup").text
    assert "SECRET" not in page and "saved" in page


def test_listing_folders_saves_the_login_but_keeps_polling_off(web, env_path, live, monkeypatch):
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


def test_choosing_a_folder_needs_the_ai_first(web, monkeypatch):
    page = web.post("/setup/mail", data={
        "folder": "Carriers", "start": "new", "host": "imap.gmail.com",
        "port": "993", "action": "test"}, headers=ORIGIN)
    assert "Set up the AI first" in page.text


def test_choosing_a_folder_from_now_on(web, env_path, live, monkeypatch, engine):
    envfile.write(env_path, {"ANTHROPIC_API_KEY": "sk", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
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


def test_the_summary_can_use_the_same_gmail_account(web, env_path, live, monkeypatch):
    envfile.write(env_path, {"IMAP_HOST": "imap.gmail.com", "IMAP_USER": "her@gmail.com",
                             "IMAP_PASSWORD": "pw"})
    live.reload()
    seen = {}
    monkeypatch.setattr("renewal.setup.checks.check_smtp",
                        lambda settings, **kw: seen.update(kw) or ok())
    web.post("/setup/summary", data={"to": "her@gmail.com", "same_account": "on",
                                     "action": "test"}, headers=ORIGIN)
    assert (seen["host"], seen["user"], seen["password"]) == ("smtp.gmail.com", "her@gmail.com", "pw")
    assert envfile.read(env_path)["SMTP_HOST"] == "smtp.gmail.com"


def test_a_key_from_the_environment_is_not_offered(engine, clean_db, env_path, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-outside")
    live = Live(env_path=env_path, external=frozenset({"ANTHROPIC_API_KEY"}))
    app = create_app(store=BlobStore(tmp_path / "b"),
                     session_factory=sessionmaker(bind=engine), live=live)
    with TestClient(app) as client:
        sign_in(client, engine)
        assert "set by the server's environment" in client.get("/setup").text
        page = client.post("/setup/model", data={"api_key": "sk", "action": "save_anyway"},
                           headers=ORIGIN)
        assert "set by the server's environment" in page.text
    assert "ANTHROPIC_API_KEY" not in envfile.read(env_path)
```

- [x] **Step 2: Run** `.venv/bin/pytest tests/test_web_setup.py -q` → FAIL.

- [x] **Step 3: Implement** `renewal/web/setup.py` with a `register(app, deps)` that defines the five routes above. Helpers inside:
  - `_render(request, *, results=None, folders=None, pending=None, status_code=200)` builds the context: `status` (`status_of(effective(...), deps.model_client)`), `s` (live settings), `saved` (dict of booleans for `ANTHROPIC_API_KEY`, `IMAP_PASSWORD`, `SMTP_PASSWORD` from `envfile.read` / settings), `external` (`deps.live.external_keys`), `results` (dict card → `CheckResult`), `folders`, `pending` (host/port/email for the folder step), `other_provider` (`s.provider != "anthropic"`), `poll` (the `MailPollState` row for `s.imap_host`/`s.imap_folder`, or `None`), `notify_to`, `update` (Task 10 fills this; pass `None` until then), `just_saved` (`request.query_params.get("saved")`).
  - `_save(updates: dict[str, str])` → `envfile.write(deps.live.env_path, updates)` then `deps.live.reload()`.
  - `_refused_external(keys)` → the first of `keys` in `deps.live.external_keys`, else `None`.
  - Checks are called as `checks.check_model(...)` etc. through the module (`from renewal.setup import checks`) so tests can replace them; `start_from_now`/`start_from_beginning` are imported by name into `renewal.web.setup`.
  - Scrubbed `result.detail` is logged with `logger.warning("setup check failed card=%s detail=%s", ...)`.

  `renewal/templates/setup.html` extends `base.html`; one `<section class="panel setupcard" id="ai|mail|summary|updates">` per card, each with a `<header>` holding the title and a status chip (`<span class="chip ok">✓ Set up</span>` / `<span class="chip">Not set up</span>`); a `.notice` / `.notice.bad` / `.notice.warn` for the card's result; forms posting to the routes with `<button class="btn primary" name="action" value="test">` and, when the card's last result failed, `<button class="btn quiet" name="action" value="save_anyway">Save anyway</button>`. Secret inputs are `type="password" autocomplete="off" value=""` with a `placeholder="••••• saved — leave blank to keep"` when saved. The mail card links `https://myaccount.google.com/apppasswords` with three lines: "Turn on 2-Step Verification if it isn't. Open App passwords, name it Renewal, click Create. Paste the 16 letters here." A `<details>` "Not Gmail?" holds host and port (defaults `imap.gmail.com`, `993`). When `folders` is set: a `<select name="folder">` and two radios `start=new` (checked, "Only mail that arrives from now on") and `start=all` ("Everything already in the folder").

  `navbadge.install`: inside the existing `try`, after the count, set
  `request.state.setup_status = status_of(effective(session, deps.settings), deps.model_client)`.
  `templating._nav`: add `"setup_status": getattr(request.state, "setup_status", None)`.
  `base.html`: add `<a href="/setup" …>Setup</a>` above Settings in the foot nav, and directly inside `<main>` before `{% block crumb %}`:

  ```jinja
  {% if setup_status and not setup_status.complete and path != "/setup" %}
  <a class="setupline" href="/setup">Finish setup ({{ setup_status.done }} of 2 done) →</a>
  {% endif %}
  ```

  `renewal/web/auth.py`: where the login redirect target defaults to `/`, if the target is `/` and `status_of(effective(session, deps.settings), deps.model_client).complete` is false, redirect to `/setup` instead.

  `app.css`: `.setupline` (a full-width strip in the signal colour's tint, ink text, 12px padding, block), `.setupcard header` flex with the chip right-aligned, `.chip` and `.chip.ok`, `.notice.warn`.

  Register in `renewal/web/__init__.py`: add `setup as setup_routes` to the imports and to `ROUTER_MODULES`.

- [x] **Step 4: Run** `.venv/bin/pytest tests/test_web_setup.py -q` → PASS; then the full suite → PASS.
- [x] **Step 5: Commit** — `feat(web): /setup, and a banner until it is done`

---

### Task 9: Knowing a newer release exists

**Files:**
- Create: `renewal/updates.py`
- Modify: `renewal/app.py` (start `UpdateClock`)
- Test: `tests/test_updates.py`

**Interfaces:**
- Produces: `parse_version(text) -> tuple[int, int, int] | None`; `current_version() -> str`; `Release(version, tag, page_url, exe_name, exe_url, sums_url)`; `fetch_latest(url, *, transport=None, timeout=10.0) -> Release`; `UpdateStatus` with `record(release)`, `record_error(message)`, `available(current: str | None = None) -> Release | None`, `checked_at`, `error`; module-level `STATUS = UpdateStatus()`; `UpdateClock(settings, status=STATUS, *, fetch=fetch_latest, sleep=time.sleep, every_seconds=43200)` with `tick()`/`start()`.

- [x] **Step 1: Write the failing tests** — `tests/test_updates.py`:

```python
import httpx
import pytest

from renewal import updates

URL = "https://api.github.com/repos/ChiltonQiu/ins/releases/latest"


def _release_json(tag="v0.3.1", assets=True):
    body = {"tag_name": tag, "html_url": f"https://github.com/ChiltonQiu/ins/releases/tag/{tag}",
            "assets": []}
    if assets:
        body["assets"] = [
            {"name": f"Renewal-{tag[1:]}-setup.exe", "browser_download_url": "https://x/exe"},
            {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"},
        ]
    return body


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("text,expected", [
    ("v0.3.1", (0, 3, 1)), ("0.10.0", (0, 10, 0)), ("v1.2", None), ("nightly", None),
])
def test_versions_parse(text, expected):
    assert updates.parse_version(text) == expected


def test_a_release_is_read():
    release = updates.fetch_latest(URL, transport=_transport(
        lambda req: httpx.Response(200, json=_release_json())))
    assert release.version == (0, 3, 1)
    assert release.exe_url == "https://x/exe" and release.sums_url == "https://x/sums"


def test_a_release_without_an_exe_has_no_exe():
    release = updates.fetch_latest(URL, transport=_transport(
        lambda req: httpx.Response(200, json=_release_json(assets=False))))
    assert release.exe_url is None


@pytest.mark.parametrize("response", [
    httpx.Response(500), httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"tag_name": "nightly"}),
])
def test_a_bad_answer_raises_for_the_clock_to_record(response):
    with pytest.raises(updates.UpdateError):
        updates.fetch_latest(URL, transport=_transport(lambda req: response))


def test_newer_is_available_and_same_or_older_is_not():
    status = updates.UpdateStatus()
    status.record(updates.Release((0, 3, 1), "v0.3.1", "p", "e", "u", "s"))
    assert status.available("0.3.0") is not None
    assert status.available("0.3.1") is None
    assert status.available("0.4.0") is None


def test_the_clock_records_a_failure_quietly():
    status = updates.UpdateStatus()

    def boom(url, **kw):
        raise updates.UpdateError("no network")

    clock = updates.UpdateClock(_settings_on(), status, fetch=boom)
    clock.tick()
    assert status.error == "no network" and status.available("0.0.1") is None


def test_the_clock_does_nothing_when_turned_off():
    calls = []
    clock = updates.UpdateClock(_settings_on(update_check=False), updates.UpdateStatus(),
                                fetch=lambda url, **kw: calls.append(url))
    clock.tick()
    assert calls == []


def _settings_on(**kw):
    import dataclasses
    from pathlib import Path
    from renewal.config import Settings
    base = Settings(database_url="x", blob_root=Path("b"), anthropic_api_key="",
                    extraction_model="m", draft_model="m", confidence_threshold=0.8,
                    materiality_config=Path("m"))
    return dataclasses.replace(base, **kw)
```

- [x] **Step 2: Run** → FAIL.

- [x] **Step 3: Implement** `renewal/updates.py` (first half — applying is Task 10):

```python
"""Whether a newer release exists, and installing it on Windows.

The check is one GET to GitHub's releases API every twelve hours on a
background thread, so no page ever waits on it; any failure is recorded and
shown as "couldn't check", never as an error page. UPDATE_CHECK=false turns
it off: it is the one request this application makes unprompted.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

import httpx

logger = logging.getLogger(__name__)

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class UpdateError(RuntimeError):
    pass


def parse_version(text: str) -> tuple[int, int, int] | None:
    found = _VERSION.fullmatch(text.strip())
    return tuple(int(part) for part in found.groups()) if found else None


def current_version() -> str:
    try:
        return version("renewal")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True)
class Release:
    version: tuple[int, int, int]
    tag: str
    page_url: str
    exe_name: str | None
    exe_url: str | None
    sums_url: str | None

    @property
    def label(self) -> str:
        return ".".join(str(part) for part in self.version)


def fetch_latest(url: str, *, transport=None, timeout: float = 10.0) -> Release:
    try:
        with httpx.Client(timeout=timeout, transport=transport,
                          headers={"Accept": "application/vnd.github+json"}) as http:
            response = http.get(url)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise UpdateError(f"could not read the latest release: {exc}") from exc
    tag = str(body.get("tag_name", ""))
    parsed = parse_version(tag)
    if parsed is None:
        raise UpdateError(f"the latest release has no version in its tag: {tag!r}")
    exe = sums = None
    for asset in body.get("assets") or []:
        name = str(asset.get("name", ""))
        if name.lower().endswith("-setup.exe"):
            exe = asset
        elif name == "SHA256SUMS":
            sums = asset
    return Release(
        version=parsed, tag=tag, page_url=str(body.get("html_url", "")),
        exe_name=exe["name"] if exe else None,
        exe_url=exe.get("browser_download_url") if exe else None,
        sums_url=sums.get("browser_download_url") if sums else None,
    )


class UpdateStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.latest: Release | None = None
        self.error: str | None = None
        self.checked_at: datetime | None = None

    def record(self, release: Release) -> None:
        with self._lock:
            self.latest, self.error = release, None
            self.checked_at = datetime.now(timezone.utc)

    def record_error(self, message: str) -> None:
        with self._lock:
            self.error = message
            self.checked_at = datetime.now(timezone.utc)

    def available(self, current: str | None = None) -> Release | None:
        mine = parse_version(current or current_version())
        latest = self.latest
        if latest is None or mine is None or latest.version <= mine:
            return None
        return latest


STATUS = UpdateStatus()


class UpdateClock:
    def __init__(self, settings, status: UpdateStatus = STATUS, *,
                 fetch=fetch_latest, sleep=time.sleep, every_seconds: int = 43200) -> None:
        self._settings, self._status = settings, status
        self._fetch, self._sleep, self._every = fetch, sleep, every_seconds

    def tick(self) -> None:
        if not self._settings.update_check:
            return
        try:
            self._status.record(self._fetch(self._settings.update_api_url))
        except UpdateError as exc:
            logger.info("update check failed: %s", exc)
            self._status.record_error(str(exc))

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a clock outlives one bad answer
                logger.exception("update tick failed")
            self._sleep(self._every)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="updates", daemon=True)
        thread.start()
        return thread
```

`renewal/app.py`: `from renewal.updates import UpdateClock` and at the end `UpdateClock(_settings).start()`.

- [x] **Step 4: Run** → PASS.
- [x] **Step 5: Commit** — `feat: know when a newer release exists`

---

### Task 10: Applying an update from the page

**Files:**
- Modify: `renewal/updates.py`, `renewal/web/setup.py`, `renewal/templates/setup.html`
- Create: `renewal/templates/updating.html`
- Test: `tests/test_updates.py`, `tests/test_web_setup.py`

**Interfaces:**
- Produces: `APP_ROOT: Path`; `can_apply(root=APP_ROOT, platform=sys.platform) -> bool`; `download_verified(release, dest_dir, *, transport=None) -> Path`; `spawn_updater(root, installer, *, popen=subprocess.Popen, pid=None) -> None`; `POST /setup/update`.

- [x] **Step 1: Write the failing tests** (append to `tests/test_updates.py`):

```python
import hashlib


def _release():
    return updates.Release((0, 3, 1), "v0.3.1", "p", "Renewal-0.3.1-setup.exe",
                           "https://x/exe", "https://x/sums")


def test_a_download_whose_checksum_matches_is_kept(tmp_path):
    payload = b"MZ installer bytes"
    sums = f"{hashlib.sha256(payload).hexdigest()}  Renewal-0.3.1-setup.exe\n".encode()
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=sums if req.url.path == "/sums" else payload))
    path = updates.download_verified(_release(), tmp_path, transport=transport)
    assert path.read_bytes() == payload and path.name == "Renewal-0.3.1-setup.exe"


def test_a_corrupted_download_is_refused_and_removed(tmp_path):
    sums = b"0" * 64 + b"  Renewal-0.3.1-setup.exe\n"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=sums if req.url.path == "/sums" else b"MZ"))
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.download_verified(_release(), tmp_path, transport=transport)
    assert not (tmp_path / "Renewal-0.3.1-setup.exe").exists()


def test_a_release_missing_its_checksum_is_refused(tmp_path):
    release = updates.Release((0, 3, 1), "v0.3.1", "p", "Renewal-0.3.1-setup.exe", "https://x/exe", None)
    with pytest.raises(updates.UpdateError):
        updates.download_verified(release, tmp_path, transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"")))


def test_one_click_is_windows_only(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "update.ps1").write_text("")
    assert updates.can_apply(tmp_path, platform="win32")
    assert not updates.can_apply(tmp_path, platform="linux")
    assert not updates.can_apply(tmp_path / "elsewhere", platform="win32")


def test_the_updater_is_started_detached_with_this_process_id(tmp_path):
    calls = []
    updates.spawn_updater(tmp_path, tmp_path / "i.exe",
                          popen=lambda args, **kw: calls.append((args, kw)), pid=4242)
    args, kw = calls[0]
    assert args[args.index("-Installer") + 1] == str(tmp_path / "i.exe")
    assert args[args.index("-AppPid") + 1] == "4242"
    assert kw["cwd"] == str(tmp_path)


def test_the_updater_retries_without_breakaway_when_the_job_forbids_it(tmp_path):
    flags = []

    def popen(args, **kw):
        flags.append(kw["creationflags"])
        if len(flags) == 1:
            raise PermissionError("access denied")

    updates.spawn_updater(tmp_path, tmp_path / "i.exe", popen=popen, pid=1)
    assert flags[0] & updates.CREATE_BREAKAWAY_FROM_JOB
    assert not flags[1] & updates.CREATE_BREAKAWAY_FROM_JOB
```

Append to `tests/test_web_setup.py`:

```python
def test_the_update_section_says_up_to_date(web, monkeypatch):
    from renewal import updates
    monkeypatch.setattr(updates, "STATUS", updates.UpdateStatus())
    assert "up to date" in web.get("/setup").text or "Couldn't check" in web.get("/setup").text


def test_update_now_hands_off_on_windows(web, monkeypatch, tmp_path):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record(updates.Release((99, 0, 0), "v99.0.0", "p", "R-99.0.0-setup.exe", "e", "s"))
    monkeypatch.setattr(updates, "STATUS", status)
    monkeypatch.setattr(updates, "can_apply", lambda *a, **k: True)
    monkeypatch.setattr(updates, "download_verified", lambda r, d, **k: tmp_path / "x.exe")
    spawned = []
    monkeypatch.setattr(updates, "spawn_updater", lambda root, exe, **k: spawned.append(exe))
    page = web.post("/setup/update", headers=ORIGIN)
    assert page.status_code == 200 and "Updating to 99.0.0" in page.text
    assert spawned == [tmp_path / "x.exe"]


def test_update_now_elsewhere_offers_the_download_instead(web, monkeypatch):
    from renewal import updates
    status = updates.UpdateStatus()
    status.record(updates.Release((99, 0, 0), "v99.0.0", "https://rel", "R.exe", "e", "s"))
    monkeypatch.setattr(updates, "STATUS", status)
    monkeypatch.setattr(updates, "can_apply", lambda *a, **k: False)
    assert "https://rel" in web.get("/setup").text
    assert web.post("/setup/update", headers=ORIGIN).status_code == 400
```

- [x] **Step 2: Run** → FAIL.

- [x] **Step 3: Implement** — append to `renewal/updates.py`:

```python
import hashlib
import os
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def can_apply(root: Path = APP_ROOT, platform: str = sys.platform) -> bool:
    return platform == "win32" and (root / "scripts" / "update.ps1").exists()


def download_verified(release: Release, dest_dir: Path, *, transport=None) -> Path:
    """The .exe, checked against the release's SHA256SUMS.

    Same release, same publisher: this proves the bytes arrived intact, not
    who made them. It is the trust somebody places in the .exe the first
    time they double-click it, and it is not called signature checking."""
    if not (release.exe_url and release.sums_url and release.exe_name):
        raise UpdateError("that release has no installer and checksum to download")
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=120.0, transport=transport, follow_redirects=True) as http:
            sums = http.get(release.sums_url)
            sums.raise_for_status()
            exe = http.get(release.exe_url)
            exe.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not download the update: {exc}") from exc
    expected = None
    for line in sums.text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == release.exe_name:
            expected = parts[0].lower()
    if expected is None:
        raise UpdateError(f"SHA256SUMS does not list {release.exe_name}")
    path = dest_dir / release.exe_name
    if hashlib.sha256(exe.content).hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise UpdateError("the download does not match its checksum; nothing was installed")
    path.write_bytes(exe.content)
    return path


def spawn_updater(root: Path, installer: Path, *, popen=subprocess.Popen, pid: int | None = None) -> None:
    """Hand off to a process that outlives this one.

    Detached, and broken away from the job object the venv launcher runs this
    interpreter in: the launcher closes that job when it exits, and a child
    still inside it would die with the application it is meant to replace."""
    args = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden", "-File", str(root / "scripts" / "update.ps1"),
        "-Installer", str(installer), "-AppPid", str(pid or os.getpid()),
    ]
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    kwargs = dict(cwd=str(root), close_fds=True, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        popen(args, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except PermissionError:
        popen(args, creationflags=base, **kwargs)
```

In `renewal/web/setup.py` add `from renewal import updates` and, in `_render`, `update = {"current": updates.current_version(), "available": updates.STATUS.available(), "error": updates.STATUS.error, "checked_at": updates.STATUS.checked_at, "can_apply": updates.can_apply(), "enabled": deps.settings.update_check}`. Add:

```python
    @router.post("/setup/update", response_class=HTMLResponse)
    def apply_update(request: Request):
        release = updates.STATUS.available()
        if release is None:
            return RedirectResponse("/setup#updates", status_code=303)
        if not updates.can_apply():
            raise HTTPException(400, "One-click update works on Windows installs. "
                                     f"Download it from {release.page_url}")
        try:
            installer = updates.download_verified(
                release, updates.APP_ROOT / "runtime" / "downloads")
            updates.spawn_updater(updates.APP_ROOT, installer)
        except updates.UpdateError as exc:
            return _render(request, results={"updates": CheckResult(False, str(exc))})
        return TEMPLATES.TemplateResponse(request, "updating.html", {"version": release.label})
```

The Updates card in `setup.html`: "Version {{ update.current }}" then one of — disabled: "Update checks are off (UPDATE_CHECK=false)."; available and can_apply: "{{ available.label }} is available — [what's new](page_url)" and a form posting `/setup/update` with a primary button "Update now" and the line "Renewal restarts itself; it takes a few minutes. A backup of the database is made first."; available and not can_apply: the link to `page_url` "Download it"; error: "Couldn't check for updates ({{ error }})."; else "Up to date ✓". The banner (`base.html`) also shows "· Renewal {{ label }} is available" when `update_available` is set; add `request.state.update_available = updates.STATUS.available()` next to the setup status in `navbadge`, and expose it in `_nav`.

`renewal/templates/updating.html` (extends `base.html`): heading "Updating to {{ version }}", the sentence "This takes a few minutes. Leave this page open — it comes back by itself.", and:

```html
<p id="state">Waiting for Renewal to stop…</p>
<script>
  // Down first, then up: a page that only waited for "up" would see the old
  // version still answering and declare victory before anything happened.
  (function () {
    var wentDown = false, started = Date.now(), state = document.getElementById("state");
    function poll() {
      fetch("/login", {cache: "no-store"}).then(function (r) {
        if (wentDown && r.ok) { location.href = "/setup#updates"; return; }
        schedule();
      }, function () {
        wentDown = true;
        state.textContent = "Installing… (this is the slow part)";
        schedule();
      });
    }
    function schedule() {
      if (Date.now() - started > 15 * 60 * 1000) {
        state.textContent = "This is taking longer than it should. The details are in runtime\\update.log in the Renewal folder.";
        return;
      }
      setTimeout(poll, 3000);
    }
    schedule();
  })();
</script>
```

- [x] **Step 4: Run** `.venv/bin/pytest tests/test_updates.py tests/test_web_setup.py -q` → PASS; full suite → PASS.
- [x] **Step 5: Commit** — `feat: update from the setup page on Windows`

---

### Task 11: `update.ps1`, the installer's words, and CI for both features

**Files:**
- Create: `scripts/update.ps1`
- Modify: `MANIFEST.in` (`include scripts/update.ps1`), `packaging/build-installer.sh` (require `scripts/update.ps1`), `packaging/renewal.nsi` (finish text), `scripts/install.ps1` ("Ready" text), `scripts/start.ps1` (the did-not-start message), `.github/workflows/windows-install.yml`
- Test: `tests/test_windows_scripts.py` (existing tests cover the new script), CI

- [x] **Step 1: Write `scripts/update.ps1`** (ASCII only):

```powershell
<#
    Install a downloaded release over this one, then start it again.

    Started by the application itself (renewal/updates.py), detached, with the
    application's own process id. In order: back up the private database, stop
    the application, run the installer silently into this folder, start the
    application again -- whether or not the install worked, so a failed update
    leaves the old version running rather than nothing. Everything goes to
    runtime\update.log.
#>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [int]$AppPid = 0
)

$ErrorActionPreference = 'Stop'

function Invoke-Native {
    # See bootstrap.ps1: under Stop, 5.1 makes redirected stderr fatal.
    param([scriptblock]$Command)
    $ErrorActionPreference = 'Continue'
    & $Command 2>&1 | ForEach-Object { "$_" }
}

Set-Location (Join-Path $PSScriptRoot '..')
$root = $PWD.Path
$runtime = Join-Path $root 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
try { Start-Transcript -Path (Join-Path $runtime 'update.log') -Append | Out-Null } catch { }

function Log { param($m) Write-Host "$(Get-Date -Format s)  $m" }

Log "update: installer $Installer, application pid $AppPid"

# ------------------------------------------------------------------ backup

$pgDump = Join-Path $runtime 'pgsql\bin\pg_dump.exe'
if (Test-Path $pgDump) {
    $conf = Join-Path $runtime 'pgdata\postgresql.conf'
    $port = 5433
    $found = Select-String -Path $conf -Pattern '^\s*port\s*=\s*(\d+)' -ErrorAction SilentlyContinue |
             Select-Object -Last 1
    if ($found) { $port = [int]$found.Matches[0].Groups[1].Value }
    $backups = Join-Path $runtime 'backups'
    New-Item -ItemType Directory -Force -Path $backups | Out-Null
    $file = Join-Path $backups ("before-update-{0}.dump" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Remove-Item Env:PGPORT, Env:PGDATA, Env:PGPASSWORD -ErrorAction SilentlyContinue
    $out = Invoke-Native { & $pgDump -h 127.0.0.1 -p $port -U postgres -Fc -f $file renewal }
    if ($LASTEXITCODE -ne 0) {
        Log "backup failed ($LASTEXITCODE): $out"
        Log 'not updating without a backup; the running version is untouched'
        try { Stop-Transcript | Out-Null } catch { }
        exit 1
    }
    Log "backup: $file"
} else {
    Log 'backup skipped: this install uses a PostgreSQL outside this folder'
}

# -------------------------------------------------------------------- stop

if ($AppPid -gt 0) {
    try {
        Stop-Process -Id $AppPid -Force -ErrorAction Stop
        Wait-Process -Id $AppPid -Timeout 30 -ErrorAction SilentlyContinue
        Log "stopped the application ($AppPid)"
    } catch {
        Log "the application ($AppPid) was not running: $($_.Exception.Message)"
    }
}

# ------------------------------------------------------------------ install

# /D= must be last and unquoted, spaces and all: that is how NSIS reads it.
$proc = Start-Process -FilePath $Installer -ArgumentList @('/S', "/D=$root") -PassThru
$null = $proc.Handle
$proc.WaitForExit()
$code = $proc.ExitCode
Log "installer exited $code"

# -------------------------------------------------------------------- start

& (Get-Command powershell).Source -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $root 'scripts\start.ps1') -NoBrowser
Log "start exited $LASTEXITCODE"

try { Stop-Transcript | Out-Null } catch { }
exit $code
```

- [x] **Step 2: Packaging.** `MANIFEST.in`: add `include scripts/update.ps1` beside `scripts/start.ps1`. `packaging/build-installer.sh`: add `scripts/update.ps1` to the `for required in …` list.

- [x] **Step 3: The installer's words.** In `packaging/renewal.nsi` replace `MUI_FINISHPAGE_TEXT` with:

```
!define MUI_FINISHPAGE_TEXT "Renewal is installed.$\r$\n$\r$\nTick the box below to make your login. Then open Renewal from the icon on the desktop: the setup page walks you through the AI key and your mail, and tests each one as you go."
```

In `scripts/install.ps1`'s `Ready` section replace the `$modelReady` branches with:

```powershell
Write-Host '  Double-click the Renewal icon on the desktop.'
Write-Host '  It opens at http://127.0.0.1:8000. The setup page there walks you'
Write-Host '  through the AI key and your mail, and tests each one as you go.'
```

and delete the "Optional … IMAP_* … SMTP_*" block's first two lines (keep the archive and schtasks lines). Leave the `Model provider` section's warning, reworded: `Warn 'No AI key yet -- the setup page asks for it.'`. In `scripts/start.ps1`, replace the "usual cause is an empty ANTHROPIC_API_KEY" paragraph with "To see the actual error, open this folder in a terminal and run …" (the key no longer stops it starting).

- [x] **Step 4: CI.** In `.github/workflows/windows-install.yml`, in the `install` job's "A key and an account" step, stop writing a fake key (the app must boot without one now) and instead append `SESSION_COOKIE_SECURE=false` and `UPDATE_CHECK=false`. Then add after "Sign in":

```yaml
      - name: Setup page, with nothing set up
        run: |
          $s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
          Invoke-WebRequest http://127.0.0.1:8000/login -Method Post -UseBasicParsing -WebSession $s `
            -Headers @{ Origin = 'http://127.0.0.1:8000' } `
            -Body @{ email = 'ci@example.com'; password = 'ci-password-long-enough' } | Out-Null
          $page = Invoke-WebRequest http://127.0.0.1:8000/setup -UseBasicParsing -WebSession $s
          if ($page.Content -notmatch 'Not set up') { throw 'the setup page did not render its cards' }
          $inbox = Invoke-WebRequest http://127.0.0.1:8000/inbox -UseBasicParsing -WebSession $s
          if ($inbox.Content -notmatch 'Finish setup') { throw 'no setup banner' }
```

In the `install-exe` job, in "A key and an account", write `SESSION_COOKIE_SECURE=false` and `UPDATE_API_URL=http://127.0.0.1:8765/latest.json` instead of a key, and add after "Start it from the desktop shortcut":

```yaml
      # The button's whole path: the check finds a "newer" release on a local
      # server, the app downloads and verifies it, spawns update.ps1 from its
      # own process tree, and is stopped, reinstalled and started by it.
      - name: One-click update
        timeout-minutes: 30
        run: |
          $exe = (Get-ChildItem setup\Renewal-*-setup.exe)[0]
          $feed = Join-Path $env:RUNNER_TEMP 'feed'
          New-Item -ItemType Directory -Force -Path $feed | Out-Null
          Copy-Item $exe.FullName (Join-Path $feed 'Renewal-99.0.0-setup.exe')
          $hash = (Get-FileHash (Join-Path $feed 'Renewal-99.0.0-setup.exe') -Algorithm SHA256).Hash.ToLower()
          [IO.File]::WriteAllText((Join-Path $feed 'SHA256SUMS'), "$hash  Renewal-99.0.0-setup.exe`n")
          $json = @{ tag_name = 'v99.0.0'; html_url = 'http://127.0.0.1:8765/'; assets = @(
            @{ name = 'Renewal-99.0.0-setup.exe'; browser_download_url = 'http://127.0.0.1:8765/Renewal-99.0.0-setup.exe' },
            @{ name = 'SHA256SUMS'; browser_download_url = 'http://127.0.0.1:8765/SHA256SUMS' }) } | ConvertTo-Json -Depth 4
          [IO.File]::WriteAllText((Join-Path $feed 'latest.json'), $json)
          Start-Process python -ArgumentList '-m','http.server','8765','--bind','127.0.0.1' -WorkingDirectory $feed -WindowStyle Hidden

          # Restart so the clock checks the local feed at start.
          Set-Location $env:APP
          Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -like '*uvicorn*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
          powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start.ps1 -NoBrowser
          if ($LASTEXITCODE -ne 0) { throw 'restart failed' }

          $s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
          Invoke-WebRequest http://127.0.0.1:8000/login -Method Post -UseBasicParsing -WebSession $s `
            -Headers @{ Origin = 'http://127.0.0.1:8000' } `
            -Body @{ email = 'ci@example.com'; password = 'ci-password-long-enough' } | Out-Null
          $deadline = (Get-Date).AddSeconds(60)
          do {
            Start-Sleep 2
            $page = Invoke-WebRequest http://127.0.0.1:8000/setup -UseBasicParsing -WebSession $s
          } until ($page.Content -match '99\.0\.0' -or (Get-Date) -gt $deadline)
          if ($page.Content -notmatch 'Update now') { throw 'the update was never offered' }

          $r = Invoke-WebRequest http://127.0.0.1:8000/setup/update -Method Post -UseBasicParsing `
            -WebSession $s -Headers @{ Origin = 'http://127.0.0.1:8000' }
          if ($r.Content -notmatch 'Updating to 99.0.0') { throw 'the button did not hand off' }

          $log = Join-Path $env:APP 'runtime\update.log'
          $deadline = (Get-Date).AddMinutes(20)
          while (-not ((Test-Path $log) -and (Select-String -Path $log -Pattern 'start exited' -Quiet))) {
            if ((Get-Date) -gt $deadline) { throw 'update.log never finished' }
            Start-Sleep 5
          }
          Get-Content $log -Tail 40
          if (-not (Select-String -Path $log -Pattern 'installer exited 0' -Quiet)) { throw 'the installer failed' }
          if (-not (Get-ChildItem (Join-Path $env:APP 'runtime\backups\*.dump'))) { throw 'no backup was made' }
          $r = Invoke-WebRequest http://127.0.0.1:8000/login -UseBasicParsing
          if ($r.StatusCode -ne 200) { throw 'not back up after the update' }
```

and add `'runtime\update.log'` to the job's final Logs step list.

- [x] **Step 5: Run** `.venv/bin/pytest tests/test_windows_scripts.py tests/test_packaging.py -q` → PASS. Push the branch; the CI run must pass all four jobs. Fix what it finds before moving on (each fix: failing test first where it can be tested locally; a CI step where it cannot).
- [x] **Step 6: Commit** (per fix, as they land) — `feat(windows): update.ps1, and CI for setup and update`

---

### Task 12: The rules, the docs, and 0.3.0

**Files:**
- Modify: `CLAUDE.md`, `renewal/settings_store.py` (docstring), `renewal/config.py` (the IMAP comment), `docs/how-it-works.md`, `README.md`, `PRIVACY.md`, `pyproject.toml` (version `0.3.0`)
- Create: `docs/releases/v0.3.0.md`

- [x] **Step 1: The rule.** In `CLAUDE.md` replace "Deployment configuration — model names, hosts, credentials, intervals — lives in the environment and never on a page." with: "Deployment configuration lives in the environment. The key, the mailbox and the sender can be typed into `/setup`, which writes them only to `.env` (`renewal/envfile.py`) and never renders them back or stores them in the database; everything else — model names, intervals, worker counts — is set in `.env` by hand." Also remove "The app builds its model client at import, so it will not boot without `PROVIDER` and the matching API key." and add "With no key the app boots on `UnconfiguredClient`; model stages fail as `ModelNotConfigured` until `/setup` has one." Mirror the rule in the `settings_store.py` docstring's second paragraph and the `config.py` comment above `imap_host` ("Credentials are deployment configuration: they never reach the database and never reach a page" → "…never reach the database; /setup may write them to .env and never shows them back").
- [x] **Step 2: Docs.** `docs/how-it-works.md`: a short "### Setup (`/setup`)" section under The screens (the three cards, the `.env` rule, live reload) and "### Updates" (check every 12h, Windows one-click, backup, `UPDATE_CHECK`). `README.md`: replace the post-install "put the key in .env" instructions with "open Renewal and follow the setup page". `PRIVACY.md`: add a paragraph on the update check (what is sent: an HTTPS GET to api.github.com, i.e. the office's IP address; how to turn it off).
- [x] **Step 3: Release notes** `docs/releases/v0.3.0.md`: what it does for somebody installing (no `.env` editing; the three cards; update from the page), what it does not (Microsoft 365; other AI providers need `.env`), and what CI covers vs what nobody has clicked.
- [x] **Step 4: Version.** `pyproject.toml` → `version = "0.3.0"`. Full suite `.venv/bin/pytest -q` → PASS.
- [ ] **Step 5: Commit** — `release: 0.3.0`; push; wait for CI green on that commit; merge to master (fast-forward), tag `v0.3.0`, build with `./packaging/build-installer.sh`, write `SHA256SUMS` for the three files, `gh release create v0.3.0` with the notes and the four assets.
  *Not done: merging to master, tagging `v0.3.0` and publishing the release are
  left for the owner. Up to that point it ran on 2026-09-28 -- 1,038 passed, 3
  deselected; Windows CI green on all four jobs, including the one-click update
  (backup made, installer exited 0, application back up). The first CI run of
  Task 11 had failed: the updater, started with DETACHED_PROCESS, never wrote
  a line; it now gets a hidden console and writes its own log.*
