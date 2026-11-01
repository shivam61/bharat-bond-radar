"""Compare BondDekho raw fetches against NSE-derived descriptor hints.

Reads data/raw/bonddekho/*.txt (parsed with india_bonds.enrichment.bonddekho) and
the descriptor hints in data/bonds.db (opened read-only). Writes one CSV row per
ISIN to data/inputs/drafts/bonddekho_compare.csv. Nothing is imported into master
data; BondDekho values are unverified hints.
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from india_bonds.enrichment.bonddekho import parse_bonddekho  # noqa: E402

RAW_DIR = REPO / "data" / "raw" / "bonddekho"
DB_PATH = REPO / "data" / "bonds.db"
OUT_PATH = REPO / "data" / "inputs" / "drafts" / "bonddekho_compare.csv"

COUPON_TOL = 0.005
COLUMNS = [
    "isin", "name", "found", "bd_rating", "bd_agency", "bd_coupon", "nse_coupon_hint",
    "coupon_status", "bd_maturity", "nse_maturity_hint", "maturity_status", "bd_face",
    "nse_face_hint", "face_status", "bd_frequency", "bd_secured", "bd_seniority",
    "bd_platforms", "notes",
]


def _status(bd, nse, equal) -> str:
    if bd is None and nse is None:
        return "missing"
    if bd is None:
        return "only_nse"
    if nse is None:
        return "only_bd"
    return "agree" if equal(bd, nse) else "conflict"


def _load_hints(conn: sqlite3.Connection) -> dict[str, dict]:
    hints: dict[str, dict] = {}
    query = """
        SELECT d.isin, d.coupon_pct, d.maturity_date, d.face_value, i.name
        FROM descriptor_hint d
        LEFT JOIN instrument i ON i.isin = d.isin
    """
    for isin, coupon, maturity, face, name in conn.execute(query):
        hints[isin] = {
            "coupon": coupon,
            "maturity": maturity,
            "face": face,
            "name": name,
        }
    return hints


def _names_only(conn: sqlite3.Connection) -> dict[str, str]:
    return {isin: name for isin, name in conn.execute("SELECT isin, name FROM instrument")}


def _platform_str(platforms: list[dict]) -> str:
    parts = []
    for p in platforms:
        ytm = "N/A" if p["ytm_pct"] is None else f"{p['ytm_pct']:g}"
        parts.append(f"{p['platform']}:{ytm}")
    return ";".join(parts)


def build_rows() -> tuple[list[dict], list[dict]]:
    """Return (rows, bd_records) where bd_records mirrors rows for summary use."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        hints = _load_hints(conn)
        names = _names_only(conn)
    finally:
        conn.close()

    rows: list[dict] = []
    records: list[dict] = []
    for path in sorted(RAW_DIR.glob("*.txt")):
        isin = path.stem
        rec = parse_bonddekho(path.read_text(encoding="utf-8"))
        hint = hints.get(isin, {})
        nse_coupon = hint.get("coupon")
        nse_maturity = hint.get("maturity")
        nse_face = hint.get("face")

        bd_maturity = rec.maturity_date.isoformat() if rec.maturity_date else None
        coupon_status = _status(rec.coupon_pct, nse_coupon,
                                lambda a, b: abs(float(a) - float(b)) <= COUPON_TOL)
        maturity_status = _status(bd_maturity, nse_maturity, lambda a, b: str(a) == str(b))
        face_status = _status(rec.face_value, nse_face,
                              lambda a, b: abs(float(a) - float(b)) < 1e-9)

        name = hint.get("name") or names.get(isin)
        row = {
            "isin": isin,
            "name": name or "",
            "found": "True" if rec.found else "False",
            "bd_rating": rec.rating or "",
            "bd_agency": rec.rating_agency or "",
            "bd_coupon": "" if rec.coupon_pct is None else rec.coupon_pct,
            "nse_coupon_hint": "" if nse_coupon is None else nse_coupon,
            "coupon_status": coupon_status,
            "bd_maturity": bd_maturity or "",
            "nse_maturity_hint": nse_maturity or "",
            "maturity_status": maturity_status,
            "bd_face": "" if rec.face_value is None else rec.face_value,
            "nse_face_hint": "" if nse_face is None else nse_face,
            "face_status": face_status,
            "bd_frequency": rec.coupon_frequency or "",
            "bd_secured": "" if rec.secured is None else str(rec.secured),
            "bd_seniority": rec.seniority or "",
            "bd_platforms": _platform_str(rec.platforms),
            "notes": ";".join(rec.warnings),
        }
        rows.append(row)
        records.append({"row": row, "rec": rec, "nse_coupon": nse_coupon,
                        "nse_maturity": nse_maturity, "nse_face": nse_face})
    return rows, records


def write_csv(rows: list[dict]) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def summarise(records: list[dict]) -> None:
    total = len(records)
    found = [r for r in records if r["rec"].found]
    print(f"ISINs read: {total}")
    print(f"found: {len(found)}  not found: {total - len(found)}")

    fields = [("coupon", "coupon_status", "bd_coupon", "nse_coupon_hint"),
              ("maturity", "maturity_status", "bd_maturity", "nse_maturity_hint"),
              ("face", "face_status", "bd_face", "nse_face_hint")]
    print("\nfield status counts:")
    conflicts: dict[str, list[tuple[str, str, str]]] = {}
    for label, status_col, bd_col, nse_col in fields:
        counts = Counter(r["row"][status_col] for r in records)
        print(f"  {label:9s} " + "  ".join(f"{k}={counts.get(k, 0)}" for k in
                                       ["agree", "conflict", "only_bd", "only_nse", "missing"]))
        conflicts[label] = [
            (r["row"]["isin"], r["row"][bd_col], r["row"][nse_col])
            for r in records if r["row"][status_col] == "conflict"
        ]

    print("\nconflicts (ISIN: BD value vs NSE hint):")
    any_conflict = False
    for label, items in conflicts.items():
        for isin, bd_val, nse_val in items:
            any_conflict = True
            print(f"  [{label}] {isin}: bd={bd_val} nse={nse_val}")
    if not any_conflict:
        print("  none")

    usable_freq = sum(1 for r in found if r["rec"].coupon_frequency is not None)
    rating_with_agency = sum(1 for r in found if r["rec"].rating and r["rec"].rating_agency)
    secured_flag = sum(1 for r in found if r["rec"].secured is not None)
    print("\nquality among found records:")
    print(f"  usable (normalised) coupon frequency: {usable_freq}/{len(found)}")
    print(f"  rating WITH agency: {rating_with_agency}/{len(found)}")
    print(f"  secured flag known: {secured_flag}/{len(found)}")


def main() -> int:
    rows, records = build_rows()
    write_csv(rows)
    summarise(records)
    print(f"\nwrote {OUT_PATH} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
