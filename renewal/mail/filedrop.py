"""A local implementation for development and tests.

Reads .eml files from a directory. It exists so the whole intake path can be
exercised without a hosted provider, a domain, or an MX record — and so the
tests that matter here are about routing, dedupe, and quarantine rather than
about someone's webhook format.
"""

from __future__ import annotations

from pathlib import Path

from renewal.mail.parse import parse_mime
from renewal.mail.provider import InboundEmail


class FileDropProvider:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def verify(self, headers: dict, body: bytes) -> bool:
        """Nothing to verify: this provider is local files, not a network
        caller. It must never be wired to a public route."""
        return True

    def parse(self, headers: dict, body: bytes) -> InboundEmail:
        return parse_mime(body)

    def drain(self) -> list[InboundEmail]:
        return [
            parse_mime(path.read_bytes())
            for path in sorted(self.directory.glob("*.eml"))
        ]
