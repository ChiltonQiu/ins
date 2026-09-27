"""Ways the Windows scripts have broken on a real machine, kept from recurring.

Windows PowerShell 5.1 reads a .ps1 without a byte-order mark as Windows-1252,
not UTF-8. An em dash is E2 80 94 in UTF-8, and 0x94 in 1252 is a closing
curly quote — which PowerShell accepts as the end of a string. The first real
install on Windows died on exactly that, at parse time, before anything ran.
cmd.exe and NSIS have the same problem with their own code pages.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

WINDOWS_FILES = sorted(
    [*ROOT.glob("scripts/*.ps1"), *ROOT.glob("*.cmd"), *ROOT.glob("packaging/*.nsi")]
)


def test_there_are_windows_files_to_check():
    assert WINDOWS_FILES


@pytest.mark.parametrize("path", WINDOWS_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_windows_file_is_ascii(path):
    bad = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.isascii()
    ]
    assert not bad, "non-ASCII where Windows will misread it:\n" + "\n".join(bad)


# pg_ctl start leaves the server running, and the server inherits pg_ctl's
# stdout. Pipe that stdout anywhere -- `| Out-Null`, a variable, 2>&1 into
# either -- and PowerShell waits for the pipe to close, which it never does
# while the database is up. The first real install hung there, forever, with
# the cluster already created and nothing on screen saying why.
_PIPED_PG_START = re.compile(r"pg_?ctl.*\bstart\b.*(\||2>&1)", re.IGNORECASE)


@pytest.mark.parametrize(
    "path", sorted(ROOT.glob("scripts/*.ps1")), ids=lambda p: p.name
)
def test_pg_ctl_start_is_never_piped(path):
    bad = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.lstrip().startswith("#") and _PIPED_PG_START.search(line)
    ]
    assert not bad, "pg_ctl start through a pipe hangs on Windows:\n" + "\n".join(bad)
