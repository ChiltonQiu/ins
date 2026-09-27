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
