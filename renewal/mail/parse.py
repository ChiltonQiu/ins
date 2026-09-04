"""MIME to the neutral shape the rest of the system uses."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime

from renewal.mail.provider import Attachment, InboundEmail

_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    return " ".join(_TAG.sub(" ", html).split())


def _received_at(message) -> datetime:
    try:
        return parsedate_to_datetime(message["Date"])
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def parse_mime(raw: bytes) -> InboundEmail:
    message = message_from_bytes(raw, policy=policy.default)

    body_parts: list[str] = []
    attachments: list[Attachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        content_type = part.get_content_type()
        if disposition == "attachment" or part.get_filename():
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                Attachment(
                    filename=part.get_filename() or "attachment",
                    content_type=content_type,
                    data=payload,
                )
            )
        elif content_type == "text/plain":
            body_parts.append(part.get_content())
        elif content_type == "text/html":
            body_parts.append(_html_to_text(part.get_content()))

    message_id = message["Message-ID"]
    if not message_id:
        # Dedupe needs a stable key. A content hash re-derives the same value
        # for a re-delivery of the same bytes, which is what Message-ID gave us.
        message_id = f"sha256:{hashlib.sha256(raw).hexdigest()}"

    return InboundEmail(
        message_id=str(message_id),
        from_address=str(message["From"] or ""),
        to_address=str(message["To"] or ""),
        subject=str(message["Subject"]) if message["Subject"] else None,
        received_at=_received_at(message),
        body_text="\n\n".join(p.strip() for p in body_parts if p.strip()),
        raw_mime=raw,
        attachments=attachments,
    )
