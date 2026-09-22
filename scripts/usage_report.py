"""Everything that happened, as JSON on stdout.

    python -m scripts.usage_report > usage.json
    python -m scripts.usage_report --days 7 --out week.json

Written to be handed to somebody who cannot see the database: a colleague, a
model, a bug report. It carries counts, distributions, durations and field
paths, and it carries no client name, no filename, no premium and no document
text. `--check` prints what it would contain without writing anything, for
whoever wants to satisfy themselves of that before sending it.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy.orm import sessionmaker

from renewal.db import get_engine
from renewal.usagestats import report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="how far back to look (default 30)")
    parser.add_argument("--out", default=None,
                        help="write here instead of stdout")
    parser.add_argument("--check", action="store_true",
                        help="print the top-level shape and sizes, write nothing")
    args = parser.parse_args(argv)

    session = sessionmaker(bind=get_engine())()
    try:
        data = report(session, days=args.days)
    finally:
        session.close()

    if args.check:
        for section, body in data.items():
            if isinstance(body, dict):
                print(f"{section}: {len(body)} keys")
            else:
                print(f"{section}: {body!r}")
        return 0

    text = json.dumps(data, indent=2, sort_keys=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"wrote {args.out} ({len(text):,} bytes, {args.days} days)",
              file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
