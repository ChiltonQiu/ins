"""Dump corrections as a labeled dataset (JSON Lines on stdout).

Usage: python scripts/export_corrections.py > corrections.jsonl

The output contains extracted client data by design — it is training data.
Treat the file the same way the blob store is treated: never commit it.
"""

from __future__ import annotations

import json
import sys

from renewal.corrections import export_rows
from renewal.db import session_scope


def main() -> None:
    with session_scope() as session:
        for row in export_rows(session):
            sys.stdout.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
