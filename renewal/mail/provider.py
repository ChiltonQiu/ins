"""The inbound-mail boundary.

The hosted provider is deliberately not chosen yet. Postmark inbound,
CloudMailin, and SES inbound all deliver the same two things — the raw MIME and
a set of headers to authenticate the request — so the record layer depends on
this interface rather than on any of them, and picking one is a single new
implementation of `InboundProvider` plus a line of wiring.

When that choice is made, record it in a comment here: which provider, why, and
what its webhook authentication actually verifies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class InboundEmail:
    message_id: str
    from_address: str
    to_address: str
    subject: str | None
    received_at: datetime
    body_text: str
    raw_mime: bytes
    attachments: list[Attachment] = field(default_factory=list)


class InboundProvider(Protocol):
    def verify(self, headers: dict, body: bytes) -> bool:
        """Whether this request genuinely came from the provider. A false
        result must quarantine, never process."""

    def parse(self, headers: dict, body: bytes) -> InboundEmail: ...
