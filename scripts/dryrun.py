"""SYNTHETIC end-to-end dry run of the screener. Never touches data/bonds.db.

Creates a fresh data/dryrun.db, imports tests/fixtures/dryrun/*.csv (all SYNTHETIC),
runs screener.run_screen(settlement=2026-10-30, run_id='dryrun'), prints a table and
compares each ISIN's bucket and rejection rule to EXPECTED (hand-written from the spec,
NOT derived from screener output). Exits non-zero on any mismatch.

Usage: .venv/bin/python scripts/dryrun.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from india_bonds.data_sources import csv_import  # noqa: E402
from india_bonds.database import db  # noqa: E402
from india_bonds.screening import screener  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "dryrun"
DB_PATH = ROOT / "data" / "dryrun.db"
SETTLEMENT = date(2026, 10, 30)
RUN_ID = "dryrun"
SYNTHETIC_PLATFORM = "SYNTHETIC PLATFORM P"

# (expected bucket, expected rejection rule or None when no rejection is expected)
EXPECTED: dict[str, tuple[str, str | None]] = {
    "INE000D00001": ("primary", None),                      # D01 AAA, annual, exec-verified
    "INE000D00002": ("primary", None),                      # D02 AA+ x2 agencies, monthly, exec-verified
    "INE000D00003": ("speculative", None),                 # D03 A+ quarterly
    "INE000D00004": ("primary", None),                     # D04 AA, clean 880 (discount 12%)
    "INE000D00005": ("rejected", "no_instrument_rating"),  # D05 no rating rows
    "INE000D00006": ("rejected", "perpetual"),             # D06 is_perpetual
    "INE000D00007": ("rejected", "at1"),                   # D07 is_at1
    "INE000D00008": ("rejected", "maturity_over_10y"),     # D08 maturity 2038
    "INE000D00009": ("rejected", "missing_coupon_or_schedule"),  # D09 coupon_frequency blank
    "INE000D00010": ("rejected", "trading_suspended"),     # D10
    "INE000D00011": ("rejected", "default_flag"),          # D11
    "INE000D00012": ("speculative", None),                 # D12 CRISIL AAA vs ICRA A+ (rating_conflict flag)
    "INE000D00013": ("rejected", "stale_price"),           # D13 price observed 2026-09-01
    "INE000D00014": ("rejected", "rating_below_A"),        # D14 BBB+
    "INE000D00015": ("primary", None),                     # D15 AA, no executable verification
    "INE000D00016": ("rejected", "unverified_is_at1"),     # D16 is_at1 blank
}
ORPHAN_RATING_ISIN = "INE000D00099"  # in ratings.csv only; must NOT create an instrument


def _fmt(value, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def main() -> int:
    if DB_PATH.exists():
        DB_PATH.unlink()
    journal = DB_PATH.with_name(DB_PATH.name + "-journal")
    if journal.exists():
        journal.unlink()

    conn = db.connect(DB_PATH)
    db.init_schema(conn)

    master = csv_import.import_instrument_master(conn, FIXTURES / "instrument_master.csv")
    ratings = csv_import.import_ratings(conn, FIXTURES / "ratings.csv")
    market = csv_import.import_marketplace(conn, FIXTURES / "marketplace.csv")
    print("== CSV imports (SYNTHETIC) ==")
    for label, rep in (("instrument_master", master), ("ratings", ratings), ("marketplace", market)):
        print(f"{label}: rows_read={rep.rows_read} rows_ok={rep.rows_ok} "
              f"duplicates={rep.duplicates} errors={len(rep.errors)}")
        for line_no, msg in rep.errors:
            print(f"  error line {line_no}: {msg}")

    # SYNTHETIC STAND-IN: CSV imports never set exchange_listed (only the NSE universe does),
    # so without this every instrument is rejected as no_active_isin.
    for isin in EXPECTED:
        db.upsert_instrument(conn, isin, exchange_listed=1)

    # SYNTHETIC: simulates a manual executable-verification of D01 and D02 (CSV import sets 0).
    for isin in ("INE000D00001", "INE000D00002"):
        db.insert_availability(
            conn, isin=isin, listed=None, recently_traded=None, offered_for_sale=None,
            price_verified=None, executable_verified=1, min_investment=None,
            platform=SYNTHETIC_PLATFORM, observed_at="2026-10-29", source_id=None,
            source_url="SYNTHETIC TEST DATA",
        )
    conn.commit()

    summary = screener.run_screen(conn, SETTLEMENT, run_id=RUN_ID)

    rows = conn.execute(
        "SELECT * FROM screen_result WHERE run_id = ? ORDER BY isin", (RUN_ID,)
    ).fetchall()
    rules_by_isin: dict[str, set[str]] = {}
    for r in conn.execute("SELECT isin, rule FROM rejection WHERE run_id = ?", (RUN_ID,)):
        rules_by_isin.setdefault(r["isin"], set()).add(r["rule"])

    header = (f"{'ISIN':<13} {'bucket':<12} {'worst':<6} {'ytm%':>7} {'dirty':>9} {'disc%':>7} "
              f"{'conf':<6} {'flags':<22} {'rejection rules':<28} check")
    print()
    print(f"== screen run_id={RUN_ID} settlement={SETTLEMENT.isoformat()} ==")
    print(header)
    print("-" * len(header))

    failures = 0
    for r in rows:
        isin = r["isin"]
        rules = rules_by_isin.get(isin, set())
        exp_bucket, exp_rule = EXPECTED[isin]
        exp_rules = set() if exp_rule is None else {exp_rule}
        ok = r["bucket"] == exp_bucket and rules == exp_rules
        failures += 0 if ok else 1
        ytm = None if r["ytm"] is None else r["ytm"] * 100
        print(
            f"{isin:<13} {r['bucket']:<12} {_fmt(r['worst_rating'], 's'):<6} "
            f"{_fmt(ytm, '7.2f'):>7} {_fmt(r['dirty_price'], '9.2f'):>9} "
            f"{_fmt(r['discount_pct'], '7.2f'):>7} {_fmt(r['confidence'], 's'):<6} "
            f"{(r['flags'] or '-'):<22} {(','.join(sorted(rules)) or '-'):<28} "
            f"{'PASS' if ok else 'FAIL'}"
        )
        if not ok:
            print(f"    expected bucket={exp_bucket} rules={sorted(exp_rules) or '-'}; "
                  f"got bucket={r['bucket']} rules={sorted(rules) or '-'}")

    # Orphan rating must be an import error and must not create an instrument.
    orphan_error = any("unknown isin" in msg for _, msg in ratings.errors)
    orphan_created = db.instrument_exists(conn, ORPHAN_RATING_ISIN)
    orphan_rows = conn.execute("SELECT COUNT(*) FROM rating WHERE isin = ?",
                               (ORPHAN_RATING_ISIN,)).fetchone()[0]
    orphan_ok = orphan_error and not orphan_created and orphan_rows == 0
    failures += 0 if orphan_ok else 1
    print()
    print(f"orphan rating {ORPHAN_RATING_ISIN}: import_error={orphan_error} "
          f"instrument_created={orphan_created} rating_rows={orphan_rows} -> "
          f"{'PASS' if orphan_ok else 'FAIL'}")

    print()
    print(f"screen summary: instruments={summary['instruments']} buckets={summary['buckets']} "
          f"ytm_computed={summary['ytm_computed']}")
    print(f"rejections by rule: {summary['rejections']}")
    print(f"checks failed: {failures}")
    conn.close()
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
