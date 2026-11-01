"""Draft worksheets built from UNVERIFIED descriptor hints (descriptor_hint table).

Writes four files into an output folder (default data/inputs/drafts, git-ignored):
  - instrument_master_DRAFT.csv: CSV in csv_import.MASTER_HEADER, only for ISINs whose hint parsed
    coupon, maturity and face value and whose instrument has no coupon_pct yet. Only the fields a
    descriptor can supply are filled; everything else is blank for the user to supply.
  - master_review.csv: one row per hinted ISIN, with what the user still has to supply.
  - worklist.md / worklist.csv: hinted bonds with a recent NSE price, for manual confirmation.
  - README.md: explains the workflow.

Optional BondDekho hints (bonddekho_dir): read from LOCAL raw files <ISIN>.txt only, never fetched.
They appear only as bd_*_unverified / bd_status / bd_*_vs_nse columns in master_review.csv and
worklist.csv, and as a markdown column in worklist.md. They are never written to the DRAFT master.

Nothing here is imported automatically, and nothing here feeds eligibility or the screener.
Hint values are NOT verified master data. Pure rendering over the database; never fetches the
CDSL or BondDekho links (they are strings for manual viewing only).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from india_bonds.data_sources.csv_import import MASTER_HEADER
from india_bonds.enrichment.bonddekho import parse_bonddekho

DRAFT_MASTER_NAME = "instrument_master_DRAFT.csv"
MASTER_REVIEW_NAME = "master_review.csv"
WORKLIST_MD_NAME = "worklist.md"
WORKLIST_CSV_NAME = "worklist.csv"
README_NAME = "README.md"

SOURCE_URL_DRAFT = "NSE descriptor (unverified draft)"
CDSL_URL = "https://www.cdslindia.com/CorporateBond/CorpBondDatabase.aspx?ISIN={isin}"
BONDDEKHO_URL = "https://www.bonddekho.com/bond/{isin}"
RISK_FLAGS = ("is_perpetual", "is_at1", "is_structured", "default_flag", "trading_suspended")
WORKLIST_HEADER_TEXT = (
    "Draft hints from NSE bond names — unverified. Confirm each bond against its term sheet / "
    "CDSL page (view manually) before copying values into data/inputs/instrument_master.csv. "
    "Coupon frequency, ratings, security and risk flags must come from you."
)

# BondDekho hint columns: appended after the existing columns. Every value is UNVERIFIED.
# A *_vs_nse conflict means the NSE-name value was kept (it stays in face_hint / maturity_hint)
# and a human must check it. BondDekho face values are never written to any column.
BD_COLUMNS = [
    "bd_status", "bd_rating_unverified", "bd_rating_agency_unverified", "bd_frequency_unverified",
    "bd_secured_unverified", "bd_seniority_unverified", "bd_face_vs_nse", "bd_maturity_vs_nse",
]
REVIEW_HEADER = [
    "isin", "issuer", "series", "coupon_hint", "maturity_hint", "face_hint", "years_to_maturity",
    "perpetual_hint", "at1_hint", "call_hint", "hint_warnings", "latest_price_date",
    "latest_price_kind", "nse_reported_yield_pct", "trades", "still_missing",
    "cdsl_reference_url", "bonddekho_reference_url", *BD_COLUMNS,
]
WORKLIST_HEADER = [
    "isin", "issuer_series", "coupon_hint", "maturity_hint", "years_left", "face_hint",
    "nse_price_per_100", "nse_reported_yield_pct_unverified", "trades", "price_date",
    "still_missing", "cdsl_reference_url", "bonddekho_reference_url", *BD_COLUMNS,
]
_SECURED_WORD = {True: "secured", False: "unsecured"}

_HINT_SQL = """
SELECT h.isin, h.raw, h.issuer, h.series, h.coupon_pct AS coupon_hint,
       h.maturity_date AS maturity_hint, h.face_value AS face_hint,
       h.perpetual_hint, h.at1_hint, h.call_hint, h.warnings, h.observed_at,
       i.coupon_pct AS master_coupon_pct, i.coupon_frequency, i.issue_date,
       i.first_interest_date, i.security_type, i.seniority,
       i.is_perpetual, i.is_at1, i.is_structured, i.default_flag, i.trading_suspended
FROM descriptor_hint h JOIN instrument i ON i.isin = h.isin
ORDER BY h.isin
"""

_LATEST_PRICE_SQL = """
SELECT p.isin, p.trade_date, p.kind, p.price_per_100, p.reported_yield_pct, p.trades
FROM price_snapshot p
WHERE p.id = (
    SELECT p2.id FROM price_snapshot p2 WHERE p2.isin = p.isin
    ORDER BY p2.trade_date DESC, p2.id DESC LIMIT 1)
"""


@dataclass
class DraftSummary:
    out_dir: Path
    as_of: date
    max_age_days: int
    hinted: int
    master_rows: int
    review_rows: int
    worklist_rows: int
    bd_status_counts: dict = field(default_factory=dict)
    bd_conflict_rows: int = 0


def _num_text(value: Optional[float]) -> str:
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _years_left(maturity_iso: Optional[str], today: date) -> Optional[float]:
    if not maturity_iso:
        return None
    return round((date.fromisoformat(maturity_iso) - today).days / 365.25, 2)


def _still_missing(r: sqlite3.Row, has_rating: bool) -> list[str]:
    out = []
    if r["coupon_frequency"] is None:
        out.append("coupon_frequency")
    if r["issue_date"] is None and r["first_interest_date"] is None:
        out.append("issue_date or first_interest_date")
    if not has_rating:
        out.append("instrument rating")
    if r["security_type"] is None:
        out.append("security_type")
    if r["seniority"] is None:
        out.append("seniority")
    out.extend(flag for flag in RISK_FLAGS if r[flag] is None)
    return out


def _has_hint_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'descriptor_hint'"
    ).fetchone() is not None


def load_hinted(conn: sqlite3.Connection, today: date) -> list[dict]:
    """One dict per hinted ISIN, with the latest price snapshot of any kind (None if unpriced)."""
    if not _has_hint_table(conn):
        return []
    latest = {r["isin"]: r for r in conn.execute(_LATEST_PRICE_SQL)}
    rated = {r["isin"] for r in conn.execute("SELECT DISTINCT isin FROM rating")}
    out = []
    for r in conn.execute(_HINT_SQL):
        p = latest.get(r["isin"])
        out.append({
            "isin": r["isin"],
            "issuer": r["issuer"],
            "series": r["series"],
            "raw": r["raw"],
            "coupon_hint": r["coupon_hint"],
            "maturity_hint": r["maturity_hint"],
            "face_hint": r["face_hint"],
            "perpetual_hint": r["perpetual_hint"],
            "at1_hint": r["at1_hint"],
            "call_hint": r["call_hint"],
            "hint_warnings": r["warnings"] or "",
            "observed_at": r["observed_at"],
            "master_coupon_pct": r["master_coupon_pct"],
            "years_to_maturity": _years_left(r["maturity_hint"], today),
            "still_missing": _still_missing(r, r["isin"] in rated),
            "latest_price_date": p["trade_date"] if p else None,
            "latest_price_kind": p["kind"] if p else None,
            "price_per_100": p["price_per_100"] if p else None,
            "nse_reported_yield_pct": p["reported_yield_pct"] if p else None,
            "trades": p["trades"] if p else None,
            "cdsl_reference_url": CDSL_URL.format(isin=r["isin"]),
            "bonddekho_reference_url": BONDDEKHO_URL.format(isin=r["isin"]),
        })
    return out


def is_draft_master_candidate(h: dict) -> bool:
    return (
        h["coupon_hint"] is not None and h["maturity_hint"] is not None
        and h["face_hint"] is not None and h["master_coupon_pct"] is None
    )


def master_draft_rows(hinted: list[dict]) -> list[dict]:
    rows = []
    for h in hinted:
        if not is_draft_master_candidate(h):
            continue
        rows.append({
            "isin": h["isin"],
            "issuer_name": h["issuer"].title() if h["issuer"] else "",
            "instrument_name": h["raw"],
            "face_value": _num_text(h["face_hint"]),
            "coupon_pct": _num_text(h["coupon_hint"]),
            "maturity_date": h["maturity_hint"],
            "currency": "INR",
            "source_url": SOURCE_URL_DRAFT,
            "retrieved_on": (h["observed_at"] or "")[:10],
        })
    return rows


def _bd_blank() -> dict:
    return {**{c: "" for c in BD_COLUMNS}, "_md": ""}


def _vs_nse(bd_value, nse_value, same) -> str:
    if bd_value is None or nse_value is None:
        return ""
    return "agree" if same(bd_value, nse_value) else "conflict"


def _bd_md_text(cells: dict, conflicts: list[str]) -> str:
    parts = [cells["bd_frequency_unverified"], cells["bd_secured_unverified"]]
    rating, agency = cells["bd_rating_unverified"], cells["bd_rating_agency_unverified"]
    if rating:
        parts.append(f"{rating} ({agency})" if agency else rating)
    parts.extend(f"CONFLICT: {name}" for name in conflicts)
    return " · ".join(p for p in parts if p)


def bonddekho_cells(isin: str, hint: dict, bd_dir: Optional[Path]) -> dict:
    """BondDekho hint cells for one ISIN, read from <bd_dir>/<ISIN>.txt. Never touches the network.

    bd_dir None disables BondDekho: every cell is blank. A missing file gives bd_status no_file;
    a file without a parsed bond gives not_found. Frequency, security and rating are shown only
    as unverified columns; the NSE values in hint stay authoritative.
    """
    if bd_dir is None:
        return _bd_blank()
    cells = _bd_blank()
    path = Path(bd_dir) / f"{isin}.txt"
    if not path.is_file():
        cells["bd_status"], cells["_md"] = "no_file", "no file"
        return cells
    rec = parse_bonddekho(path.read_text(encoding="utf-8", errors="replace"))
    if not rec.found:
        cells["bd_status"], cells["_md"] = "not_found", "not found"
        return cells
    cells["bd_status"] = "found"
    cells["bd_rating_unverified"] = rec.rating or ""
    cells["bd_rating_agency_unverified"] = rec.rating_agency or ""
    cells["bd_frequency_unverified"] = rec.coupon_frequency or ""
    cells["bd_secured_unverified"] = _SECURED_WORD.get(rec.secured, "")
    cells["bd_seniority_unverified"] = rec.seniority or ""
    cells["bd_face_vs_nse"] = _vs_nse(rec.face_value, hint.get("face_hint"),
                                      lambda a, b: abs(float(a) - float(b)) < 1e-9)
    bd_maturity = rec.maturity_date.isoformat() if rec.maturity_date else None
    cells["bd_maturity_vs_nse"] = _vs_nse(bd_maturity, hint.get("maturity_hint"), lambda a, b: a == b)
    conflicts = [name for name, col in (("face", "bd_face_vs_nse"), ("maturity", "bd_maturity_vs_nse"))
                 if cells[col] == "conflict"]
    cells["_md"] = _bd_md_text(cells, conflicts)
    return cells


def _flag(value: Optional[int]) -> str:
    return "" if value is None else str(int(value))


def review_rows(hinted: list[dict]) -> list[dict]:
    rows = []
    for h in hinted:
        rows.append({
            "isin": h["isin"],
            "issuer": h["issuer"] or "",
            "series": h["series"] or "",
            "coupon_hint": _num_text(h["coupon_hint"]),
            "maturity_hint": h["maturity_hint"] or "",
            "face_hint": _num_text(h["face_hint"]),
            "years_to_maturity": "" if h["years_to_maturity"] is None else f"{h['years_to_maturity']:.2f}",
            "perpetual_hint": _flag(h["perpetual_hint"]),
            "at1_hint": _flag(h["at1_hint"]),
            "call_hint": _flag(h["call_hint"]),
            "hint_warnings": h["hint_warnings"],
            "latest_price_date": h["latest_price_date"] or "",
            "latest_price_kind": h["latest_price_kind"] or "",
            "nse_reported_yield_pct": _num_text(h["nse_reported_yield_pct"]),
            "trades": "" if h["trades"] is None else str(h["trades"]),
            "still_missing": "; ".join(h["still_missing"]),
            "cdsl_reference_url": h["cdsl_reference_url"],
            "bonddekho_reference_url": h["bonddekho_reference_url"],
            **h.get("bd", _bd_blank()),
        })
    return rows


def worklist(hinted: list[dict], today: date, max_age_days: int) -> list[dict]:
    """Hinted bonds whose latest price snapshot is within max_age_days, best yield first."""
    cutoff = (today - timedelta(days=max_age_days)).isoformat()
    picked = [h for h in hinted if h["latest_price_date"] and h["latest_price_date"] >= cutoff
              # a hinted maturity on/before today means a stale descriptor or a matured bond: not a candidate
              and not (h["maturity_hint"] and h["maturity_hint"] <= today.isoformat())]

    def key(h: dict):
        y, t = h["nse_reported_yield_pct"], h["trades"]
        return (y is None, -(y or 0.0), t is None, -(t or 0), h["isin"])

    rows = []
    for h in sorted(picked, key=key):
        issuer = h["issuer"].title() if h["issuer"] else ""
        rows.append({
            "isin": h["isin"],
            "issuer_series": " ".join(x for x in (issuer, h["series"] or "") if x),
            "coupon_hint": _num_text(h["coupon_hint"]),
            "maturity_hint": h["maturity_hint"] or "",
            "years_left": "" if h["years_to_maturity"] is None else f"{h['years_to_maturity']:.2f}",
            "face_hint": _num_text(h["face_hint"]),
            "nse_price_per_100": _num_text(h["price_per_100"]),
            "nse_reported_yield_pct_unverified": _num_text(h["nse_reported_yield_pct"]),
            "trades": "" if h["trades"] is None else str(h["trades"]),
            "price_date": h["latest_price_date"] or "",
            "still_missing": "; ".join(h["still_missing"]),
            "_missing_list": h["still_missing"],
            "cdsl_reference_url": h["cdsl_reference_url"],
            "bonddekho_reference_url": h["bonddekho_reference_url"],
            **h.get("bd", _bd_blank()),
        })
    return rows


def _csv_text(header: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, restval="", lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


_SHORT_LABELS = {
    "coupon_frequency": "frequency",
    "issue_date or first_interest_date": "issue/first-interest date",
    "instrument rating": "rating",
    "security_type": "security",
    "seniority": "seniority",
}


def _still_missing_short(items: list[str]) -> str:
    """Compact form for the markdown table; the CSV keeps the full list."""
    out = [_SHORT_LABELS.get(x, x) for x in items if x not in RISK_FLAGS]
    flags = [x for x in items if x in RISK_FLAGS]
    if flags:
        out.append(f"risk flags {len(flags)}/{len(RISK_FLAGS)}")
    return "; ".join(out)


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _worklist_md(rows: list[dict], as_of: date, max_age_days: int) -> str:
    cutoff = (as_of - timedelta(days=max_age_days)).isoformat()
    lines = [
        "# Draft worklist: hinted bonds with a recent NSE price",
        "",
        f"**{WORKLIST_HEADER_TEXT}**",
        "",
        f"As of {as_of.isoformat()}. Prices dated {cutoff} or later ({max_age_days} day window). "
        "Sorted by NSE reported traded yield (desc), then trades (desc). "
        f"Rows: {len(rows)}.",
        "",
        "| ISIN | Issuer / series | Coupon hint % | Maturity hint | Years left | Face hint | "
        "NSE price per 100 | NSE traded yield % (unverified) | Trades | Price date | "
        "Still missing | BondDekho (unverified) | Reference links |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not rows:
        lines.append("| _no rows_ |  |  |  |  |  |  |  |  |  |  |  |  |")
    for r in rows:
        links = f"[CDSL]({r['cdsl_reference_url']}) · [BondDekho]({r['bonddekho_reference_url']})"
        cells = [
            r["isin"], r["issuer_series"], r["coupon_hint"], r["maturity_hint"], r["years_left"],
            r["face_hint"], r["nse_price_per_100"], r["nse_reported_yield_pct_unverified"],
            r["trades"], r["price_date"], _still_missing_short(r["_missing_list"]),
            r.get("_md", ""), links,
        ]
        lines.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


def _readme(as_of: date) -> str:
    return f"""# Draft worksheets from NSE descriptor hints

Generated {as_of.isoformat()} by `scripts/enrich.py draft`. Rerunning overwrites these files.
This folder is git-ignored (it sits under data/).

## What the hints are

NSE bond names (for example `REC LIMITED SR 261 8.3 BD 31OT31 FVRS1LAC`) carry a coupon, a
maturity code, and a face value. They are parsed into DRAFT, UNVERIFIED hints. Descriptors can be
wrong or stale: for example, descriptor `28FB27` for one Muthoot MCRED bond differs from its CDSL
redemption date of 08-Mar-2027. Hints never carry payout frequency, issue date, rating, security
type, or risk flags.

## Files

- `instrument_master_DRAFT.csv`: the instrument master header, filled only for ISINs whose hint has
  coupon, maturity and face value and that have no coupon in the database yet. Filled: isin,
  issuer_name (hint issuer, title-cased), instrument_name (the raw descriptor), face_value,
  coupon_pct, maturity_date, currency, source_url, retrieved_on (date the hint was first seen).
  `currency` is set to INR because the descriptor's FVRS token marks an Indian-rupee face value. That
  is an inference from the descriptor, not a verified fact. Every other column is blank on purpose.
  `is_perpetual` stays blank even where the hint says perpetual.
- `master_review.csv`: one row per hinted ISIN, with the hint values, years to maturity, the latest
  NSE price snapshot, and `still_missing`, the list of fields you must supply.
- `worklist.md` and `worklist.csv`: hinted bonds with an NSE price in the window, best reported yield
  first. The yield is NSE's own figure and is unverified.
- The `bd_*_unverified` columns (and `bd_status`, `bd_face_vs_nse`, `bd_maturity_vs_nse`) in
  `master_review.csv` and `worklist.csv` hold BondDekho aggregator hints. They are unverified and are
  never written to `instrument_master_DRAFT.csv`. `bd_status` is found, not_found, or no_file.
- A `CONFLICT` in `bd_face_vs_nse` or `bd_maturity_vs_nse` means BondDekho disagrees with the NSE-name
  value. The NSE value was kept in `face_hint` or `maturity_hint`; check it by hand on the CDSL page.

## Workflow

1. Open each ISIN in `master_review.csv`, or in `worklist.md`. Check the coupon, maturity and face
   value against the term sheet or the CDSL page (`cdsl_reference_url`). View these pages manually;
   the tools never fetch them.
2. Fill the missing fields from your own sources: coupon frequency, issue date or first interest
   date, instrument rating (with agency, date and source), security type, seniority, and the five
   risk flags (is_perpetual, is_at1, is_structured, default_flag, trading_suspended).
3. Copy only the values you have confirmed into `data/inputs/instrument_master.csv`, then run
   `scripts/refresh.py`. Do not import `instrument_master_DRAFT.csv` as it is: it lacks frequency,
   flags and ratings, so the screener rejects those bonds.

Nothing here feeds eligibility or the screener, and nothing is imported automatically.
"""


def write_drafts(conn: sqlite3.Connection, out_dir, *, today: Optional[date] = None,
                 max_age_days: int = 7, bonddekho_dir=None) -> DraftSummary:
    """Write the five draft files into out_dir (overwritten). Returns counts.

    bonddekho_dir: folder of local BondDekho raw files (<ISIN>.txt). None (default) disables the
    BondDekho columns. Files are read only; this function makes no network calls.
    """
    if today is None:
        from india_bonds.refresh import ist_today
        today = ist_today()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    hinted = load_hinted(conn, today)
    for h in hinted:
        h["bd"] = bonddekho_cells(h["isin"], h, bonddekho_dir)
    bd_status_counts = dict(Counter(h["bd"]["bd_status"] for h in hinted if h["bd"]["bd_status"]))
    bd_conflict_rows = sum(
        1 for h in hinted
        if "conflict" in (h["bd"]["bd_face_vs_nse"], h["bd"]["bd_maturity_vs_nse"]))
    master = master_draft_rows(hinted)
    review = review_rows(hinted)
    work = worklist(hinted, today, max_age_days)

    (out / DRAFT_MASTER_NAME).write_text(_csv_text(MASTER_HEADER, master), encoding="utf-8")
    (out / MASTER_REVIEW_NAME).write_text(_csv_text(REVIEW_HEADER, review), encoding="utf-8")
    (out / WORKLIST_CSV_NAME).write_text(_csv_text(WORKLIST_HEADER, work), encoding="utf-8")
    (out / WORKLIST_MD_NAME).write_text(_worklist_md(work, today, max_age_days), encoding="utf-8")
    (out / README_NAME).write_text(_readme(today), encoding="utf-8")

    return DraftSummary(
        out_dir=out, as_of=today, max_age_days=max_age_days, hinted=len(hinted),
        master_rows=len(master), review_rows=len(review), worklist_rows=len(work),
        bd_status_counts=bd_status_counts, bd_conflict_rows=bd_conflict_rows,
    )
