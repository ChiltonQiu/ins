"""The IMAP connection, against a fake server.

Read-only is the property worth testing here: this connects to somebody's real
mail, and the guarantee that it cannot alter it should be a test rather than a
comment.
"""

import pytest

from renewal.mail.imapbox import Mailbox


class FakeIMAP:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.validity = b"12345"

    def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b""]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def examine(self, folder):
        self.calls.append(("examine", folder))
        return "OK", [b"1"]

    def status(self, folder, what):
        return "OK", [b'"' + folder.encode() + b'" (UIDVALIDITY ' + self.validity + b")"]

    def uid(self, command, *args):
        self.calls.append(("uid", command) + tuple(str(a) for a in args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        if command == "FETCH":
            uid = int(args[0])
            raw = self.messages[uid]
            return "OK", [(b"1 (RFC822 {%d}" % len(raw), raw), b")"]
        raise AssertionError(command)

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]


def _box(fake):
    return Mailbox("imap.example.com", "anne", "app-password",
                   folder="Carriers", connector=lambda host, port: fake)


def test_it_opens_the_folder_read_only():
    """EXAMINE, never SELECT. The server itself then refuses a write, so this
    cannot mark, move or delete anybody's mail even by accident."""
    fake = FakeIMAP()
    with _box(fake):
        pass

    assert ("examine", "Carriers") in fake.calls
    assert not any(call[0] == "select" for call in fake.calls)


def test_it_logs_out_even_when_the_body_raises():
    fake = FakeIMAP()
    with pytest.raises(RuntimeError):
        with _box(fake):
            raise RuntimeError("boom")

    assert ("logout",) in fake.calls


def test_uids_since_asks_only_for_what_is_new():
    fake = FakeIMAP()
    fake.messages = {4: b"a", 5: b"b"}
    with _box(fake) as box:
        assert box.uids_since(3) == [4, 5]

    assert any("UID 4:*" in " ".join(call) for call in fake.calls
               if call[0] == "uid" and call[1] == "SEARCH")


def test_uids_since_nothing_reads_the_whole_folder():
    fake = FakeIMAP()
    fake.messages = {1: b"a", 2: b"b"}
    with _box(fake) as box:
        assert box.uids_since(None) == [1, 2]


def test_a_uid_we_already_have_is_not_returned_again():
    """"UID n:*" is inclusive of the highest UID even when nothing is newer,
    so the server answers with the message we already read."""
    fake = FakeIMAP()
    fake.messages = {7: b"a"}
    with _box(fake) as box:
        assert box.uids_since(7) == []


def test_fetch_returns_the_raw_message():
    fake = FakeIMAP()
    fake.messages = {7: b"From: a@b\r\n\r\nhello"}
    with _box(fake) as box:
        assert box.fetch(7) == b"From: a@b\r\n\r\nhello"


def test_uid_validity_is_read_from_the_folder():
    fake = FakeIMAP()
    with _box(fake) as box:
        assert box.uid_validity() == 12345


def test_a_refused_login_says_what_the_server_said():
    from renewal.mail.imapbox import MailboxError

    fake = FakeIMAP()
    fake.login = lambda user, password: ("NO", [b"AUTHENTICATIONFAILED"])
    with pytest.raises(MailboxError, match="AUTHENTICATIONFAILED"):
        with _box(fake):
            pass
