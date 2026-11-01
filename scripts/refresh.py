"""One command: fetch sources, update the database, screen, and write the day's report.

Usage:
    .venv/bin/python scripts/refresh.py [--db data/bonds.db] [--date YYYY-MM-DD]
        [--offline] [--no-corp-trades] [--no-hints] [--inputs data/inputs] [--out reports]
        [--no-report] [--init-inputs]

--date defaults to today in UTC+5:30. --offline skips all network connectors and only
imports CSV inputs, screens, and reports. --no-corp-trades skips the NSE OTC/RFQ source. --no-hints
skips the descriptor hints step (UNVERIFIED hints parsed from NSE bond names; DB-only). --init-inputs
writes header-only CSV templates into the inputs folder (never overwrites) and exits without network access.

Exit codes: 0 normal (unavailable sources are warnings), 2 database cannot be opened or is
an older DB that must be rebuilt (see SchemaRebuildRequired), 1 unexpected error (traceback printed).

Cron example (weekdays 18:30 IST):
    30 18 * * 1-5 cd /path/india-bonds && .venv/bin/python scripts/refresh.py >> data/refresh.log 2>&1

Research classification only. Nothing here is investment advice.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from india_bonds.database.db import SchemaRebuildRequired  # noqa: E402
from india_bonds.refresh import (  # noqa: E402
    DatabaseOpenError,
    format_summary,
    init_inputs,
    ist_today,
    run_refresh,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_DB = 2


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch, update, screen and report in one run.")
    parser.add_argument("--db", default="data/bonds.db", help="SQLite database (default data/bonds.db)")
    parser.add_argument("--date", type=_iso_date, default=None,
                        help="settlement / trade date YYYY-MM-DD (default: today, UTC+5:30)")
    parser.add_argument("--offline", action="store_true", help="skip all network connectors")
    parser.add_argument("--no-corp-trades", action="store_true",
                        help="skip the NSE OTC/RFQ corporate bond trades source")
    parser.add_argument("--no-hints", action="store_true",
                        help="skip the descriptor hints step (unverified hints from NSE bond names)")
    parser.add_argument("--inputs", default="data/inputs", help="folder with optional CSV inputs")
    parser.add_argument("--out", default="reports", help="report output root (default reports)")
    parser.add_argument("--no-report", action="store_true", help="do not write the report folder")
    parser.add_argument("--init-inputs", action="store_true",
                        help="create header-only CSV templates in the inputs folder, then exit")
    args = parser.parse_args(argv)

    if args.init_inputs:
        try:
            results = init_inputs(args.inputs)
        except OSError:
            traceback.print_exc()
            return EXIT_UNEXPECTED
        for fname, state in results.items():
            print(f"{fname}: {state}")
        return EXIT_OK

    settlement = args.date or ist_today()
    try:
        summary = run_refresh(
            args.db, settlement, args.offline, args.inputs, args.out, make_report=not args.no_report,
            corp_trades=not args.no_corp_trades, hints=not args.no_hints,
        )
    except DatabaseOpenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DB
    except SchemaRebuildRequired as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DB
    except Exception:  # noqa: BLE001 - unexpected: show the traceback, exit 1
        traceback.print_exc()
        return EXIT_UNEXPECTED

    print(format_summary(summary))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
