import dataclasses
from pathlib import Path

import httpx
import pytest

from renewal import updates
from renewal.config import Settings

URL = "https://api.github.com/repos/ChiltonQiu/ins/releases/latest"


def _settings_on(**kw):
    base = Settings(database_url="x", blob_root=Path("b"), anthropic_api_key="",
                    extraction_model="m", draft_model="m", confidence_threshold=0.8,
                    materiality_config=Path("m"))
    return dataclasses.replace(base, **kw)


def _release_json(tag="v0.3.1", assets=True):
    body = {"tag_name": tag,
            "html_url": f"https://github.com/ChiltonQiu/ins/releases/tag/{tag}",
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
    assert release.version == (0, 3, 1) and release.label == "0.3.1"
    assert release.exe_url == "https://x/exe" and release.sums_url == "https://x/sums"


def test_a_release_without_an_exe_has_no_exe():
    release = updates.fetch_latest(URL, transport=_transport(
        lambda req: httpx.Response(200, json=_release_json(assets=False))))
    assert release.exe_url is None


@pytest.mark.parametrize("response", [
    httpx.Response(500), httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"tag_name": "nightly"}),
    httpx.Response(200, json=["not", "an", "object"]),
])
def test_a_bad_answer_raises_for_the_clock_to_record(response):
    with pytest.raises(updates.UpdateError):
        updates.fetch_latest(URL, transport=_transport(lambda req: response))


def test_no_network_raises_for_the_clock_to_record():
    def refuse(request):
        raise httpx.ConnectError("no route to host")
    with pytest.raises(updates.UpdateError):
        updates.fetch_latest(URL, transport=_transport(refuse))


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

    updates.UpdateClock(_settings_on(), status, fetch=boom).tick()
    assert status.error == "no network" and status.available("0.0.1") is None


def test_the_clock_does_nothing_when_turned_off():
    calls = []
    clock = updates.UpdateClock(_settings_on(update_check=False), updates.UpdateStatus(),
                                fetch=lambda url, **kw: calls.append(url))
    clock.tick()
    assert calls == []


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


def test_a_corrupted_download_is_refused_and_not_kept(tmp_path):
    sums = b"0" * 64 + b"  Renewal-0.3.1-setup.exe\n"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=sums if req.url.path == "/sums" else b"MZ"))
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.download_verified(_release(), tmp_path, transport=transport)
    assert not (tmp_path / "Renewal-0.3.1-setup.exe").exists()


def test_a_release_missing_its_checksum_is_refused(tmp_path):
    release = updates.Release((0, 3, 1), "v0.3.1", "p", "Renewal-0.3.1-setup.exe",
                              "https://x/exe", None)
    with pytest.raises(updates.UpdateError):
        updates.download_verified(release, tmp_path, transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"")))


def test_a_checksum_file_that_does_not_list_the_exe_is_refused(tmp_path):
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=b"abc  something-else.exe\n" if req.url.path == "/sums" else b"MZ"))
    with pytest.raises(updates.UpdateError, match="does not list"):
        updates.download_verified(_release(), tmp_path, transport=transport)


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
    assert kw["creationflags"] & updates.CREATE_NO_WINDOW
    assert kw["stdout"].name == str(tmp_path / "runtime" / "update.out")


def test_the_updater_gets_a_console_because_powershell_needs_one(tmp_path):
    """DETACHED_PROCESS left powershell.exe with no host: nothing ran, and
    nothing was logged."""
    calls = []
    updates.spawn_updater(tmp_path, tmp_path / "i.exe",
                          popen=lambda args, **kw: calls.append(kw), pid=1)
    assert not calls[0]["creationflags"] & 0x00000008


def test_the_updater_retries_without_breakaway_when_the_job_forbids_it(tmp_path):
    flags = []

    def popen(args, **kw):
        flags.append(kw["creationflags"])
        if len(flags) == 1:
            raise PermissionError("access denied")

    updates.spawn_updater(tmp_path, tmp_path / "i.exe", popen=popen, pid=1)
    assert flags[0] & updates.CREATE_BREAKAWAY_FROM_JOB
    assert not flags[1] & updates.CREATE_BREAKAWAY_FROM_JOB
