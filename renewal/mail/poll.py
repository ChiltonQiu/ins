"""One pass over the mailbox.

What is new, hand each message to the intake that already exists, remember how
far we got. The remembering is an optimisation and nothing more: correctness
lives on InboundMessage's unique (agency_id, message_id), so losing the mark
costs a re-read and never a duplicate.

The mark advances past a message that could not be parsed. Leaving it behind
would jam every later message in the folder behind one malformed one, which is
a worse failure than losing the ability to retry something that is not going to
parse on the second attempt either — and the raw bytes are in the blob store
regardless, so nothing is actually lost.

Nothing here raises. A mailbox that is refusing connections is a thing to
report on a page, not a thing to bring down a clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from renewal.blobstore import BlobStore
from renewal.config import Settings
from renewal.mail.imapbox import Mailbox
from renewal.mail.intake import receive
from renewal.mail.parse import parse_mime
from renewal.models import Agency, InboundMessage, MailPollState

logger = logging.getLogger(__name__)

# There is no tenancy yet; one mailbox, one folder, one agency.
AGENCY_ID = 1


@dataclass(frozen=True)
class PollResult:
    seen: int = 0
    ingested: int = 0
    duplicate: int = 0
    failed: int = 0


def _open(settings: Settings) -> Mailbox:
    return Mailbox(
        settings.imap_host, settings.imap_user, settings.imap_password,
        folder=settings.imap_folder, port=settings.imap_port,
    )


def _state(session: Session, settings: Settings) -> MailPollState:
    row = session.scalar(
        select(MailPollState)
        .where(MailPollState.host == settings.imap_host)
        .where(MailPollState.folder == settings.imap_folder)
    )
    if row is None:
        row = MailPollState(host=settings.imap_host, folder=settings.imap_folder)
        session.add(row)
        session.flush()
    return row


def poll_once(
    session: Session,
    store: BlobStore,
    *,
    settings: Settings,
    client=None,
    on_document=None,
    opener=_open,
) -> PollResult:
    """Read what is new and hand it to intake. Does not commit — the caller
    does, the way every other service here leaves the transaction alone."""
    # An empty host turns polling off, the same way an empty SMTP_HOST turns
    # notifications off: a half-configured mailbox must never be able to cost
    # anything.
    if not (settings.imap_host and settings.imap_user):
        return PollResult()

    state = _state(session, settings)
    agency = session.get(Agency, AGENCY_ID)
    seen = ingested = duplicate = failed = 0

    try:
        with opener(settings) as box:
            validity = box.uid_validity()
            since = state.last_uid
            if state.uid_validity is not None and state.uid_validity != validity:
                logger.info(
                    "mailbox uidvalidity changed folder=%s %s -> %s",
                    settings.imap_folder, state.uid_validity, validity,
                )
                since = None
            state.uid_validity = validity

            for uid in box.uids_since(since):
                seen += 1
                try:
                    # Each message in its own savepoint. Catching the exception
                    # is not enough on its own: a failed statement leaves the
                    # transaction aborted, so without this the first bad
                    # message takes every message after it in the same poll —
                    # which is the failure this is supposed to prevent. Real
                    # mail hits this: a stray NUL byte in a body is something
                    # Postgres refuses outright.
                    with session.begin_nested():
                        parsed = parse_mime(box.fetch(uid))
                        # Asked before receive() rather than inferred after it:
                        # receive() returns the row it already had for a
                        # duplicate, and that row says 'processed', so
                        # afterwards the two cases are indistinguishable.
                        already = session.scalar(
                            select(InboundMessage.id)
                            .where(InboundMessage.agency_id == agency.id)
                            .where(
                                InboundMessage.message_id == parsed.message_id
                            )
                        )
                        message = receive(
                            session, store, parsed, client=client,
                            settings=settings, on_document=on_document,
                            # The mailbox is the routing: this logged into one
                            # account and read one folder, so whose mail it is
                            # was settled before the envelope was opened.
                            agency=agency,
                        )
                        if already is not None:
                            duplicate += 1
                        elif message.processing_status == "processed":
                            ingested += 1
                        else:
                            failed += 1
                except Exception:  # noqa: BLE001 - one message never stops a poll
                    logger.exception(
                        "mail poll failed uid=%s folder=%s",
                        uid, settings.imap_folder,
                    )
                    failed += 1
                state.last_uid = (
                    uid if state.last_uid is None else max(state.last_uid, uid)
                )
        state.last_error = None
    except Exception as failure:  # noqa: BLE001 - reported, never raised
        logger.exception("mail poll failed host=%s", settings.imap_host)
        state.last_error = f"{type(failure).__name__}: {failure}"

    state.last_polled_at = datetime.now(timezone.utc)
    state.last_seen = seen
    state.last_ingested = ingested
    session.flush()
    return PollResult(seen=seen, ingested=ingested, duplicate=duplicate,
                      failed=failed)
