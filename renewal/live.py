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
