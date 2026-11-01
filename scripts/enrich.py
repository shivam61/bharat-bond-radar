"""Draft worksheets from UNVERIFIED NSE descriptor hints. Read-only on the database.

Usage:
    .venv/bin/python scripts/enrich.py draft [--db data/bonds.db] [--out data/inputs/drafts]
        [--max-age-days 7]

Writes instrument_master_DRAFT.csv, master_review.csv, worklist.md, worklist.csv and README.md
into --out (overwritten on rerun). Nothing is imported into the database or the screener.

Exit codes: 0 ok, 2 database file not found, 1 unexpected error (traceback printed).
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from india_bonds.database import db  # noqa: E402
from india_bonds.enrichment.drafts import write_drafts  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_DB = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft worksheets from unverified NSE descriptor hints.")
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft", help="write draft master CSV, review CSV and worklist")
    draft.add_argument("--db", default="data/bonds.db", help="SQLite database (default data/bonds.db)")
    draft.add_argument("--out", default="data/inputs/drafts", help="output folder (default data/inputs/drafts)")
    draft.add_argument("--max-age-days", type=int, default=7,
                       help="worklist: include bonds whose latest price is this many days old or newer")
    args = parser.parse_args(argv)

    if args.max_age_days < 0:
        parser.error("--max-age-days must be >= 0")
    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return EXIT_DB

    conn = db.connect(args.db)
    try:
        s = write_drafts(conn, args.out, max_age_days=args.max_age_days)
    except Exception:  # noqa: BLE001 - show the traceback, exit 1
        traceback.print_exc()
        return EXIT_UNEXPECTED
    finally:
        conn.close()

    print(f"draft as_of={s.as_of.isoformat()} out={s.out_dir}")
    print(f"hinted ISINs: {s.hinted}")
    print(f"instrument_master_DRAFT.csv rows: {s.master_rows}")
    print(f"master_review.csv rows: {s.review_rows}")
    print(f"worklist rows (price within {s.max_age_days} day(s)): {s.worklist_rows}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
