"""No router may keep the settings or the model client it was registered with.

/setup changes both under a running application, through Live. A router
that copied `deps.settings` or `deps.model_client` into a local at
registration keeps the startup value for the life of the process: uploads
and retries went on using the "not set up" client after a key was saved.
Inside a route function (deeper indentation) the copy is per request and fine.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_CAPTURE = re.compile(r"^    \w+ = deps\.(settings|model_client)\s*$")


def captures(text: str) -> list[str]:
    return [line for line in text.splitlines() if _CAPTURE.match(line)]


@pytest.mark.parametrize("path", sorted((ROOT / "renewal" / "web").glob("*.py")),
                         ids=lambda p: p.name)
def test_no_router_captures_settings_at_registration(path):
    assert not captures(path.read_text()), path.name


def test_the_check_catches_the_old_shape():
    old = "def register(app, deps):\n    model_client = deps.model_client\n"
    assert captures(old)
