"""What every router needs from the application that mounts it.

Passed explicitly into register() rather than read from a module global, so
import order is irrelevant and a test can build a second app with different
dependencies in the same process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from renewal.blobstore import BlobStore
from renewal.config import Settings


@dataclass(frozen=True)
class Deps:
    settings: Settings
    store: BlobStore
    model_client: Any
    session_factory: Any
    # Where work that must not block the response goes. The application builds
    # a ThreadRunner; tests pass an InlineRunner so their assertions are not
    # racing a thread.
    runner: Any = None


def acting_user_id(request) -> int | None:
    """Who is making this request, for a row that records a decision.

    getattr with a default rather than a bare attribute read: the .ics feed and
    the mail webhook never pass the gate and have no user on their state.
    Neither writes an attributed row today, and this is what keeps it safe if
    one ever does.
    """
    return getattr(request.state, "user_id", None)
