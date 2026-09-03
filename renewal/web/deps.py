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
