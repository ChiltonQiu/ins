"""Whether a newer release exists, and installing it on Windows.

The check is one GET to GitHub's releases API every twelve hours on a
background thread, so no page ever waits on it; any failure is recorded and
shown as "couldn't check", never as an error page. UPDATE_CHECK=false turns
it off: it is the one request this application makes unprompted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

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


# ------------------------------------------------------------- applying one

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
        with httpx.Client(timeout=120.0, transport=transport,
                          follow_redirects=True) as http:
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
        raise UpdateError(
            "the download does not match its checksum; nothing was installed")
    path.write_bytes(exe.content)
    return path


def spawn_updater(root: Path, installer: Path, *, popen=subprocess.Popen,
                  pid: int | None = None) -> None:
    """Hand off to a process that outlives this one.

    Detached, and broken away from the job object the venv launcher runs this
    interpreter in: the launcher closes that job when it exits, and a child
    still inside it would die with the application it is meant to replace.
    A job that forbids breaking away refuses the flag with access denied, and
    then the plain detached start is the best there is."""
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
