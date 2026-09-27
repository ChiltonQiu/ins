"""Changing .env from the setup page, and nothing else in it.

The file is also hand-edited, and the installer writes to it, so this touches
only the lines for the keys it is given: every comment, blank and unrelated
setting comes back out byte for byte. A value is always written double-quoted
with backslash and quote escaped, which is the one form python-dotenv reads
back exactly whatever a password contains.

UTF-8 with no byte-order mark: python-dotenv reads a BOM as part of the first
key's name, and that setting silently stops existing (scripts/install.ps1
learned this first).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

# The keys /setup writes. Everything else in the file is the installer's or
# the operator's.
OWNED = frozenset({
    "PROVIDER", "ANTHROPIC_API_KEY",
    "IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD", "IMAP_FOLDER",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFY_FROM",
})

_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("a setting cannot contain a line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {
        key: (value or "")
        for key, value in dotenv_values(path, encoding="utf-8-sig").items()
    }


def write(path: Path, updates: Mapping[str, str]) -> None:
    rendered = {key: f"{key}={quote(value)}" for key, value in updates.items()}
    # Bytes, not read_text: text mode turns \r\n into \n, and a file the
    # Windows installer wrote would come back with every line ending changed.
    text = path.read_bytes().decode("utf-8-sig") if path.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"

    out: list[str] = []
    done: set[str] = set()
    for line in text.splitlines(keepends=True):
        match = _KEY.match(line)
        key = match.group(1) if match else None
        if key in rendered:
            if key in done:
                continue  # a later duplicate would win over the edit
            ending = line[len(line.rstrip("\r\n")):] or newline
            out.append(rendered[key] + ending)
            done.add(key)
        else:
            out.append(line)

    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += newline
    for key, line in rendered.items():
        if key not in done:
            out.append(line + newline)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes("".join(out).encode("utf-8"))
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
