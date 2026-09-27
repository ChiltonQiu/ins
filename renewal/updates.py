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
    found = _VERSION.fullmatch((text or "").strip())
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
        with httpx.Client(
            timeout=timeout, transport=transport,
            headers={"Accept": "application/vnd.github+json"},
        ) as http:
            response = http.get(url)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise UpdateError(f"could not read the latest release: {exc}") from exc
    if not isinstance(body, dict):
        raise UpdateError("the latest release is not in the expected shape")
    tag = str(body.get("tag_name", ""))
    parsed = parse_version(tag)
    if parsed is None:
        raise UpdateError(f"the latest release has no version in its tag: {tag!r}")
    exe = sums = None
    for asset in body.get("assets") or []:
        if not isinstance(asset, dict):
            continue
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
                 fetch=fetch_latest, sleep=time.sleep,
                 every_seconds: int = 43200) -> None:
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
