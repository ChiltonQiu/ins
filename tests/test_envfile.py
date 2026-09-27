"""Editing .env without disturbing anything the page does not own."""

import pytest
from dotenv import dotenv_values

from renewal import envfile

HOSTILE = [
    "plain",
    "has#hash",
    "has 'single' quotes",
    'has "double" quotes',
    "has=equals",
    "  spaces  around  ",
    "back\\slash",
    "trailing\\",
    "\\\\double-backslash",
    "café-ünicode",
    "$HOME-not-expanded",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_value_round_trips(tmp_path, value):
    path = tmp_path / ".env"
    envfile.write(path, {"IMAP_PASSWORD": value})
    assert dotenv_values(path)["IMAP_PASSWORD"] == value
    assert envfile.read(path)["IMAP_PASSWORD"] == value


def test_lines_it_does_not_own_are_left_byte_for_byte(tmp_path):
    path = tmp_path / ".env"
    before = (
        "# a comment\r\n"
        "DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5433/renewal\r\n"
        "\r\n"
        "IMAP_HOST=old.example.com\r\n"
        "BLOB_ENCRYPTION_KEY=abc=\r\n"
    )
    path.write_bytes(before.encode())
    envfile.write(path, {"IMAP_HOST": "imap.gmail.com"})
    after = path.read_bytes().decode()
    assert after == before.replace(
        "IMAP_HOST=old.example.com", 'IMAP_HOST="imap.gmail.com"'
    )


def test_a_missing_key_is_appended(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    envfile.write(path, {"IMAP_USER": "her@gmail.com"})
    assert path.read_text() == 'A=1\nIMAP_USER="her@gmail.com"\n'


def test_a_missing_file_is_created(tmp_path):
    path = tmp_path / ".env"
    envfile.write(path, {"IMAP_USER": "x"})
    assert envfile.read(path) == {"IMAP_USER": "x"}


def test_a_later_duplicate_is_removed_so_it_cannot_win(tmp_path):
    path = tmp_path / ".env"
    path.write_text("IMAP_HOST=a\nOTHER=1\nIMAP_HOST=b\n")
    envfile.write(path, {"IMAP_HOST": "c"})
    assert path.read_text() == 'IMAP_HOST="c"\nOTHER=1\n'


def test_no_byte_order_mark_is_written_and_one_is_dropped(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes("﻿A=1\n".encode("utf-8"))
    envfile.write(path, {"IMAP_USER": "x"})
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert envfile.read(path)["A"] == "1"


def test_a_value_with_a_newline_is_refused(tmp_path):
    with pytest.raises(ValueError):
        envfile.write(tmp_path / ".env", {"IMAP_PASSWORD": "a\nb"})


def test_a_failed_write_leaves_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=1\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(envfile.os, "replace", boom)
    with pytest.raises(OSError):
        envfile.write(path, {"A": "2"})
    assert path.read_text() == "A=1\n"


def test_reading_a_missing_file_is_empty(tmp_path):
    assert envfile.read(tmp_path / "nope.env") == {}
