"""The IMAP connection.

Raw bytes out, nothing else. It knows about mailboxes and UIDs and knows
nothing about documents, agencies or the pipeline, which is what lets a test
drive it with a fake object rather than a TLS stack and somebody's server.

Read-only on purpose, and structurally rather than by intention: the folder is
opened with EXAMINE, so the server itself refuses any write this code could
issue. It is reading a person's actual mail, and the guarantee that it cannot
damage it should not rest on nobody ever making a mistake in this file.
"""

from __future__ import annotations

import imaplib
import logging
import re

logger = logging.getLogger(__name__)

_UIDVALIDITY = re.compile(rb"UIDVALIDITY\s+(\d+)")


class MailboxError(RuntimeError):
    """The mailbox refused something, carrying the server's own words.

    Worth keeping verbatim: 'AUTHENTICATIONFAILED' and 'no such folder' are the
    two most likely problems here, and both are fixed by reading exactly what
    the server said rather than a sentence this code made up about it.
    """


def _connect(host: str, port: int):
    return imaplib.IMAP4_SSL(host, port)


class Mailbox:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        folder: str = "INBOX",
        port: int = 993,
        connector=_connect,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._folder = folder
        self._connector = connector
        self._imap = None

    def __enter__(self) -> "Mailbox":
        self._imap = self._connector(self._host, self._port)
        try:
            self._check(self._imap.login(self._user, self._password), "login")
            # EXAMINE rather than SELECT: read-only at the protocol level.
            self._check(
                self._imap.examine(self._folder), f"examine {self._folder}"
            )
        except Exception:
            # A connection opened and then abandoned holds a session on the
            # server until it times out, and a mail provider counts those.
            self._logout()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._logout()

    def _logout(self) -> None:
        if self._imap is None:
            return
        try:
            self._imap.logout()
        except Exception:  # noqa: BLE001 - a failed logout is not the caller's problem
            logger.warning("imap logout failed host=%s", self._host)
        self._imap = None

    @staticmethod
    def _check(response, what: str):
        status, data = response
        if status != "OK":
            raise MailboxError(f"{what}: {status} {data!r}")
        return data

    def uid_validity(self) -> int:
        """The folder's generation number.

        If it changes, every UID remembered from before refers to a folder that
        no longer exists, and reading from that mark would skip real mail.
        """
        data = self._check(
            self._imap.status(self._folder, "(UIDVALIDITY)"), "status"
        )
        joined = b" ".join(
            part if isinstance(part, bytes) else str(part).encode()
            for part in data
        )
        found = _UIDVALIDITY.search(joined)
        if not found:
            raise MailboxError(f"no UIDVALIDITY in {joined!r}")
        return int(found.group(1))

    def uids_since(self, uid: int | None) -> list[int]:
        """Everything newer than uid, or the whole folder when given None."""
        criterion = "ALL" if uid is None else f"UID {uid + 1}:*"
        data = self._check(self._imap.uid("SEARCH", None, criterion), "search")
        found = [int(part) for part in (data[0] or b"").split()]
        # "UID n:*" is inclusive of the highest existing UID even when nothing
        # is newer than n, so the server can answer with mail already read.
        return sorted(u for u in found if uid is None or u > uid)

    def fetch(self, uid: int) -> bytes:
        data = self._check(
            self._imap.uid("FETCH", str(uid), "(RFC822)"), f"fetch {uid}"
        )
        for part in data:
            if isinstance(part, tuple) and len(part) > 1:
                return part[1]
        raise MailboxError(f"fetch {uid}: no message body in {data!r}")
