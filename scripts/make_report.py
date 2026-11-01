"""Write day-wise Markdown reports for the latest (or a given) screening run.

Usage:
    .venv/bin/python scripts/make_report.py --db data/bonds.db [--out reports] [--run-id X]

Output: <out>/<YYYY-MM-DD>/ (settlement date of the run) plus <out>/INDEX.md.
Read-only on the database. Research classification only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from india_bonds.dashboard import queries as q  # noqa: E402
from india_bonds.reports.markdown import NoRunError, write_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write day-wise Markdown bond screener reports.")
    parser.add_argument("--db", default="data/bonds.db", help="SQLite database (default data/bonds.db)")
    parser.add_argument("--out", default="reports", help="output root (default reports)")
    parser.add_argument("--run-id", default=None, help="run to report (default: latest run)")
    args = parser.parse_args(argv)

    conn = q.open_readonly(args.db)
    if conn is None:
        print(f"error: database not found or unreadable: {args.db}", file=sys.stderr)
        return 2
    try:
        result = write_report(conn, args.out, run_id=args.run_id)
    except NoRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"run {result.run_id} settlement {result.settlement}")
    print(f"wrote {len(result.files)} files to {result.day_dir}")
    print(f"index: {result.index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
