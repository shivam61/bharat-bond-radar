"""Write day-wise Markdown reports for one screening run.

Research classification only. Nothing here scores, grades risk, or recommends a bond.
All reads go through india_bonds.dashboard.queries; this module only formats and writes files.

Output folder: <out>/<settlement YYYY-MM-DD>/ (settlement from run_id 'screen-<date>', else the
run's created_at date). Output is deterministic except the 'Generated at' line in README.md.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from dateutil.relativedelta import relativedelta

from india_bonds.dashboard import queries as q
from india_bonds.screening.eligibility import (
    MAX_REMAINING_YEARS,
    PRIMARY_MAX_RANK,
    RANK,
    STALE_PRICE_DAYS,
)

DISCLAIMER = (
    "Research classification only — not investment advice; no transactions are executed. "
    "Phase 1: eligibility screening only; no scoring/DRCS/risk grade yet."
)
NOT_ASSESSED = "Not assessed in Phase 1 (requires financial analysis)"
DASH = "—"
NONE_THIS_RUN = "None this run."
LOW_CONFIDENCE_TAG = "**LOW CONFIDENCE**"
REJECTED_CAP_TRIGGER = 2000
REJECTED_CAP = 500
BONDS_DIR = "bonds"
SUMMARY_NAME = "summary.json"
INDEX_NAME = "INDEX.md"
SCHEMA_VERSION = 1
DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Instrument fields the eligibility master-completeness check requires (eligibility._master_complete).
MASTER_REQUIRED = (
    "face_value", "coupon_pct", "coupon_frequency", "maturity_date", "currency",
    "is_perpetual", "is_at1", "is_structured", "default_flag", "trading_suspended",
)
# Key-facts fields listed as missing on a bond page.
KEY_FIELDS = (
    "issuer_name", "name", "face_value", "coupon_pct", "coupon_frequency", "maturity_date",
    "issue_date", "first_interest_date", "security_type", "seniority", "currency",
)
FLAG_EXPLAIN = {
    "rating_conflict": "agencies disagree by two or more notches or straddle the AA-/A+ cut-off "
                       "(flag only; lowers confidence)",
    "credit_enhanced": "rating carries a (CE)/(SO) credit-enhancement qualifier",
    "agency_withdrawn": "an agency's latest action is a withdrawal; that agency is ignored",
    "rating_watch": "rating is on watch",
    "rating_qualifier": "rating carries a qualifier in parentheses",
    "reported_yield_mismatch": "our computed YTM differs from the NSE-reported traded yield by more than "
                               "0.25 percentage points; the NSE price basis is unverified, so confidence is "
                               "capped at Medium",
}
DISCOUNT_FLAGS = (("below_98", "98%"), ("below_95", "95%"), ("below_90", "90%"))

SHORTLIST_CSV_COLUMNS = [
    "rank", "run_id", "isin", "bucket", "issuer", "name", "worst_rating", "coupon_pct",
    "coupon_frequency", "maturity_date", "security_type", "our_ytm_pct", "vendor_ytm_pct",
    "nse_reported_yield_pct",
    "clean_price", "dirty_price", "discount_pct", "next_interest_date", "flags", "confidence",
    "price_date", "price_kind", "min_investment", "executable_verified", "offered_for_sale",
    "availability_observed_at",
]
REJECTED_CSV_COLUMNS = ["isin", "issuer", "name", "rule_count", "rules", "reasons"]
PERCENT_CSV_COLUMNS = {"coupon_pct", "our_ytm_pct", "vendor_ytm_pct", "nse_reported_yield_pct", "clean_price", "dirty_price",
                       "discount_pct", "min_investment"}


class NoRunError(ValueError):
    """The database has no screening run to report on."""


# ---------------------------------------------------------------- cell formatting

def is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def md(value) -> str:
    """Markdown table-safe text: missing -> em dash, newlines -> spaces, pipes escaped."""
    if is_missing(value):
        return DASH
    text = re.sub(r"\r\n|\r|\n", " ", str(value)).strip()
    return text.replace("|", "\\|")


def num(value, dp: int = 2) -> str:
    """Fixed-decimal number for Markdown; missing stays an em dash, never a default number."""
    if is_missing(value):
        return DASH
    try:
        return f"{float(value):.{dp}f}"
    except (TypeError, ValueError):
        return md(value)


def csv_num(value) -> str:
    if is_missing(value):
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def yes_no(value) -> str:
    return q.flag_text(value)


def _split_flags(value) -> list[str]:
    if is_missing(value):
        return []
    return [f.strip() for f in str(value).split(",") if f.strip()]


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def _bond_link(isin) -> str:
    return f"[{md(isin)}]({BONDS_DIR}/{isin}.md)"


def _confidence_cell(conf) -> str:
    text = md(conf)
    return f"{text} {LOW_CONFIDENCE_TAG}" if conf == "Low" else text


# ---------------------------------------------------------------- run and data

@dataclass(frozen=True)
class RunInfo:
    run_id: str
    settlement: str          # YYYY-MM-DD; also the day folder name
    created_at: str | None
    settlement_from: str     # 'run_id' or 'created_at'


@dataclass(frozen=True)
class ReportResult:
    run_id: str
    settlement: str
    day_dir: Path
    files: tuple[str, ...]
    index_path: Path


@dataclass
class ReportData:
    run: RunInfo
    primary: pd.DataFrame
    speculative: pd.DataFrame
    income: pd.DataFrame
    discounted: pd.DataFrame
    rejected: pd.DataFrame
    rule_counts: pd.DataFrame
    counts: dict
    latest_price_date: str | None
    details: dict


def _run_stats(conn, run_id: str) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS c FROM screen_result WHERE run_id = ?", (run_id,)
    ).fetchone()
    return int(row["n"]), (None if row["c"] is None else str(row["c"]))


def resolve_run(conn, run_id: str | None = None) -> RunInfo:
    """Latest run (or the given run_id). Raises NoRunError when there is nothing to report."""
    if not q.has_table(conn, "screen_result"):
        raise NoRunError("database has no screen_result table; run the screener first")
    if run_id is None:
        meta = q.latest_run(conn)
        if meta is None:
            raise NoRunError("database has no screening run; run the screener first")
        run_id = meta["run_id"]
    n, created = _run_stats(conn, run_id)
    if n == 0:
        raise NoRunError(f"run {run_id!r} has no screen_result rows")
    stored = None
    if q.has_table(conn, "screen_result"):
        try:
            row = conn.execute(
                "SELECT MAX(settlement) FROM screen_result WHERE run_id = ?", (run_id,)
            ).fetchone()
            stored = row[0] if row and row[0] else None
        except Exception:  # older DB without the column
            stored = None
    if stored:
        date.fromisoformat(stored)
        return RunInfo(run_id, stored, created, "stored")
    parsed = q.settlement_from_run_id(run_id)
    if parsed:
        return RunInfo(run_id, parsed, created, "run_id")
    if not created:
        raise NoRunError(f"run {run_id!r} has neither a settlement in its run_id nor created_at")
    day = created[:10]
    date.fromisoformat(day)  # validates the fallback before it becomes a folder name
    return RunInfo(run_id, day, created, "created_at")


def collect(conn, run: RunInfo) -> ReportData:
    rid = run.run_id
    primary = q.add_rank(q.shortlist(conn, rid, "primary"))
    speculative = q.add_rank(q.shortlist(conn, rid, "speculative"))
    total = len(q.all_isins(conn, rid))
    counts = {"primary": len(primary), "speculative": len(speculative)}
    counts["rejected"] = total - counts["primary"] - counts["speculative"]
    counts["total"] = total

    priced = pd.concat([primary, speculative], ignore_index=True)
    dates = [str(d) for d in priced["price_date"] if not is_missing(d)] if not priced.empty else []
    latest = max(dates) if dates else None

    income = q.income(conn, rid)
    if not income.empty:
        income = income.sort_values(["ytm_pct", "isin"], ascending=[False, True], na_position="last")
    discounted = q.discounted(conn, rid)
    if not discounted.empty:
        discounted = discounted.sort_values(["discount_pct", "isin"], ascending=[False, True],
                                            na_position="last")
    rejected = q.rejected(conn, rid).sort_values("isin").reset_index(drop=True)

    details = {}
    for isin in priced["isin"] if not priced.empty else []:
        details[isin] = q.isin_detail(conn, isin, rid)

    return ReportData(
        run=run, primary=primary, speculative=speculative, income=income.reset_index(drop=True),
        discounted=discounted.reset_index(drop=True), rejected=rejected,
        rule_counts=q.rejection_counts(conn, rid), counts=counts, latest_price_date=latest,
        details=details,
    )


# ---------------------------------------------------------------- README sections

SHORTLIST_HEADERS = [
    "Rank", "ISIN", "Issuer", "Lowest instrument rating", "Coupon (%)", "Our YTM (%)",
    "Vendor YTM (unverified) (%)", "NSE reported traded yield (unverified) (%)", "Clean price",
    "Dirty price", "Discount (%)", "Maturity",
    "Payout", "Next interest date", "Confidence",
]


def _shortlist_table(df: pd.DataFrame) -> str:
    if df.empty:
        return NONE_THIS_RUN
    rows = []
    for _, r in df.iterrows():
        rows.append([
            str(int(r["rank"])), _bond_link(r["isin"]), md(r["issuer"]), md(r["worst_rating"]),
            num(r["coupon_pct"]), num(r["ytm_pct"]), num(r["vendor_ytm"]), num(r["nse_reported_yield"]),
            num(r["clean_price"]), num(r["dirty_price"]), num(r["discount_pct"]),
            md(r["maturity_date"]), md(r["coupon_frequency"]), md(r["next_interest_date"]),
            _confidence_cell(r["confidence"]),
        ])
    return _table(SHORTLIST_HEADERS, rows)


def _income_table(df: pd.DataFrame) -> str:
    if df.empty:
        return NONE_THIS_RUN
    rows = []
    for _, r in df.iterrows():
        rows.append([
            _bond_link(r["isin"]), md(r["issuer"]), md(q.BUCKET_LABELS.get(r["bucket"], r["bucket"])),
            md(r["coupon_frequency"]), num(r["coupon_pct"]), num(r["ytm_pct"]),
            md(r["next_interest_date"]), _confidence_cell(r["confidence"]),
        ])
    return _table(["ISIN", "Issuer", "Bucket", "Payout", "Coupon (%)", "Our YTM (%)",
                   "Next interest date", "Confidence"], rows)


def _discounted_table(df: pd.DataFrame) -> str:
    if df.empty:
        return NONE_THIS_RUN
    rows = []
    for _, r in df.iterrows():
        rows.append([
            _bond_link(r["isin"]), md(r["issuer"]), md(q.BUCKET_LABELS.get(r["bucket"], r["bucket"])),
            num(r["clean_price"]), num(r["discount_pct"]),
            "Yes" if r["below_98"] else "No",
            "Yes" if r["below_95"] else "No",
            "Yes" if r["below_90"] else "No",
            _confidence_cell(r["confidence"]),
        ])
    return _table(["ISIN", "Issuer", "Bucket", "Clean price", "Discount (%)", "Below 98%",
                   "Below 95%", "Below 90%", "Confidence"], rows)


def _rejected_summary(rule_counts: pd.DataFrame) -> str:
    if rule_counts.empty:
        return NONE_THIS_RUN
    rows = [[md(r["rule"]), str(int(r["count"]))] for _, r in rule_counts.iterrows()]
    return _table(["Rule", "Rejected ISINs"], rows)


def _readme(data: ReportData, generated_at: str) -> str:
    run = data.run
    if run.settlement_from == "created_at":
        settle_note = " (custom run_id: taken from the run's created_at date)"
    else:
        settle_note = ""
    freshness = (
        f"latest price_date used = {data.latest_price_date} (shortlisted bonds)"
        if data.latest_price_date else "no shortlisted bond has a price in this run"
    )
    c = data.counts
    parts = [
        f"# Bond screener report — {run.settlement}",
        "",
        f"- **Settlement date:** {run.settlement}{settle_note}",
        f"- **Run ID:** `{md(run.run_id)}`",
        f"- **Data freshness:** {freshness}",
        f"- **Generated at:** {generated_at}",
        "",
        f"> **{DISCLAIMER}**",
        "",
        "## Counts per bucket",
        "",
        _table(["Bucket", "Instruments"], [
            ["Primary (AAA to AA-)", str(c["primary"])],
            ["Speculative (A+/A)", str(c["speculative"])],
            ["Rejected", str(c["rejected"])],
            ["Total screened", str(c["total"])],
        ]),
        "",
        "## Primary shortlist",
        "",
        "Ranked by our computed YTM, highest first. This is a yield ordering, not a quality ranking: a higher YTM usually reflects higher risk. Vendor YTM and NSE reported traded yield are unverified and are not used for ranking.",
        "",
        _shortlist_table(data.primary),
        "",
        "## Speculative watchlist (A+/A)",
        "",
        "Shown separately from the primary shortlist.",
        "",
        _shortlist_table(data.speculative),
        "",
        "## Monthly & quarterly income",
        "",
        _income_table(data.income),
        "",
        "## Discounted bonds (<98/95/90% of face)",
        "",
        _discounted_table(data.discounted),
        "",
        "## Rejected summary",
        "",
        _rejected_summary(data.rule_counts),
        "",
        "Per-ISIN reasons: [rejected.md](rejected.md). Full data: [rejected.csv](rejected.csv).",
        "",
        "## Files",
        "",
        "[shortlist.csv](shortlist.csv) · [speculative.csv](speculative.csv) · "
        "[rejected.csv](rejected.csv) · [rejected.md](rejected.md) · [summary.json](summary.json)",
        "",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- bond pages

def _accrued(clean, dirty):
    try:
        return float(dirty) - float(clean)
    except (TypeError, ValueError):
        return None


def _master_gaps(master: dict) -> list[str]:
    gaps = [f for f in MASTER_REQUIRED if is_missing(master.get(f))]
    if is_missing(master.get("first_interest_date")) and is_missing(master.get("issue_date")):
        gaps.append("issue_date or first_interest_date")
    return gaps


def _why_passed(master: dict, row: pd.Series, bucket: str, run: RunInfo) -> list[str]:
    out = []
    if master.get("exchange_listed") == 1:
        out.append("Marked exchange-listed in the instrument master (exchange_listed = 1).")
    if master.get("currency") == "INR":
        out.append("Currency is INR.")
    try:
        mat = date.fromisoformat(str(master.get("maturity_date"))[:10])
        settle = date.fromisoformat(run.settlement)
        if settle < mat <= settle + relativedelta(years=MAX_REMAINING_YEARS):
            out.append(f"Maturity {mat.isoformat()} is after settlement {settle.isoformat()} "
                       f"and within {MAX_REMAINING_YEARS} years.")
    except (TypeError, ValueError):
        pass
    for field_name, label in (
        ("is_perpetual", "Not perpetual"), ("is_at1", "Not AT1"),
        ("is_structured", "Not structured"), ("default_flag", "No default flag"),
        ("trading_suspended", "Trading not suspended"),
    ):
        if master.get(field_name) == 0:
            out.append(f"{label} ({field_name} = 0).")
    worst = md(row["worst_rating"])
    if bucket == "primary":
        out.append(f"Lowest instrument rating {worst} is within AAA to AA- (primary band).")
    else:
        out.append(f"Lowest instrument rating {worst} is A+ or A (speculative band).")
    if not is_missing(row["next_interest_date"]):
        out.append("Coupon schedule built from verified frequency, dates and coupon; "
                   f"next interest date {row['next_interest_date']}.")
    if not is_missing(row["price_date"]):
        out.append(f"Latest price dated {row['price_date']} ({md(q.price_kind_label(row['price_kind']))}) is within "
                   f"{STALE_PRICE_DAYS} days of settlement.")
    out.append("Our YTM is computed from the clean price, coupon and schedule; vendor YTM is not used.")
    return out


def _concerns(master: dict, row: pd.Series, avail: dict) -> list[str]:
    out = []
    for f in _split_flags(row["flags"]):
        explain = FLAG_EXPLAIN.get(f)
        out.append(f"`{f}`: {explain}" if explain else f"`{f}`")
    disc = row["discount_pct"]
    if not is_missing(disc):
        d = float(disc)
        for key, pct in DISCOUNT_FLAGS:
            cutoff = {"below_98": 2, "below_95": 5, "below_90": 10}[key]
            if d > cutoff:
                out.append(f"Discount flag `{key}`: clean price is below {pct} of face "
                           f"(discount {d:.2f}%).")
    if row["confidence"] != "High":
        gaps = _master_gaps(master)
        if gaps:
            out.append(f"Confidence reason: master fields incomplete ({', '.join(gaps)}).")
        if avail and avail.get("executable_verified") != 1:
            out.append("Confidence reason: executable not verified "
                       "(availability.executable_verified is not 1).")
    schedule_dates = ("issue_date", "first_interest_date")
    missing = [f for f in KEY_FIELDS if f not in schedule_dates and is_missing(master.get(f))]
    if all(is_missing(master.get(f)) for f in schedule_dates):
        missing.append("issue_date or first_interest_date")
    if missing:
        out.append(f"Missing fields: {', '.join(missing)}.")
    return out


def _sources_table(detail: dict) -> str:
    df = detail.get("sources")
    if df is None or df.empty:
        return "No sources recorded."
    def _safe_url(v):
        # Local file paths (CSV imports) would leak the machine's directory layout into published
        # reports; show only the file name for anything that is not an http(s) URL.
        t = "" if is_missing(v) else str(v)
        if t.startswith("/") or t.startswith("~") or (len(t) > 2 and t[1] == ":"):
            return "file: " + t.replace("\\", "/").rsplit("/", 1)[-1]
        return v

    seen = dict.fromkeys(
        (md(r["origin"]), md(_safe_url(r["url"])), md(r["fetched_at"]), md(r["status"]))
        for _, r in df.iterrows()
        if not all(is_missing(r[c]) for c in ("url", "fetched_at", "status"))
    )
    rows = [list(k) for k in seen]
    return _table(["Origin", "Source URL", "Retrieved / observed", "Status"], rows)


def render_bond_page(row: pd.Series, detail: dict, run: RunInfo, bucket: str) -> str:
    master = detail.get("master") or {}
    ratings = detail.get("ratings")
    avail_df = detail.get("availability")
    avail = avail_df.iloc[0].to_dict() if avail_df is not None and not avail_df.empty else {}
    isin = row["isin"]
    label = q.BUCKET_LABELS.get(bucket, bucket)
    conf = row["confidence"]

    facts = [
        ("Issuer", md(master.get("issuer_name") or row["issuer"])),
        ("ISIN", md(isin)),
        ("Instrument name", md(master.get("name") or row["name"])),
        ("Instrument type", md(master.get("instrument_type"))),
        ("Exchange listed", yes_no(master.get("exchange_listed"))),
        ("Bucket", md(label)),
        ("Lowest instrument rating", md(row["worst_rating"])),
        ("Coupon (%)", num(master.get("coupon_pct"))),
        ("Coupon frequency", md(master.get("coupon_frequency"))),
        ("Face value", num(master.get("face_value"))),
        ("Maturity date", md(master.get("maturity_date"))),
        ("Issue date", md(master.get("issue_date"))),
        ("First interest date", md(master.get("first_interest_date"))),
        ("Security type", md(master.get("security_type"))),
        ("Seniority", md(master.get("seniority"))),
        ("Collateral", md(master.get("collateral_text"))),
        ("Currency", md(master.get("currency"))),
    ]
    if not is_missing(master.get("coupon_hint")):
        facts.append(("Coupon hint from instrument name (unverified)", num(master.get("coupon_hint"))))

    rating_rows = []
    if ratings is not None and not ratings.empty:
        for _, r in ratings.iterrows():
            rating_rows.append([
                md(r["agency"]), md(r["rating"]), md(r["outlook"]), md(r["action"]),
                md(r["action_date"]), md(r["source_type"]), md(r["source_ref"]),
            ])
    rating_block = (
        _table(["Agency", "Rating", "Outlook", "Action", "Action date", "Source type", "Source ref"],
               rating_rows)
        if rating_rows else "No instrument rating rows recorded."
    )

    accrued = _accrued(row["clean_price"], row["dirty_price"])
    pricing = [
        ("Clean price", num(row["clean_price"])),
        ("Accrued interest (dirty − clean)", num(accrued)),
        ("Dirty price", num(row["dirty_price"])),
        ("Discount (%)", num(row["discount_pct"])),
        ("Our computed YTM (%)", num(row["ytm_pct"])),
        ("Vendor YTM (unverified) (%)", num(row["vendor_ytm"])),
        ("NSE reported traded yield (unverified) (%)", num(row["nse_reported_yield"])),
        ("Price date", md(row["price_date"])),
        ("Price kind", md(q.price_kind_label(row["price_kind"]))),
        ("Next interest date", md(row["next_interest_date"])),
    ]

    if avail:
        availability = [
            ("Listed", yes_no(avail.get("listed"))),
            ("Recently traded", yes_no(avail.get("recently_traded"))),
            ("Offered for sale", yes_no(avail.get("offered_for_sale"))),
            ("Price verified", yes_no(avail.get("price_verified"))),
            ("Executable verified", yes_no(avail.get("executable_verified"))),
            ("Min investment", num(avail.get("min_investment"))),
            ("Availability observed at", md(avail.get("observed_at"))),
        ]
    else:
        availability = [(k, "Unknown") for k in (
            "Listed", "Recently traded", "Offered for sale", "Price verified",
            "Executable verified")] + [("Min investment", DASH), ("Availability observed at", DASH)]

    why = _why_passed(master, row, bucket, run)
    concerns = _concerns(master, row, avail)
    if conf != "High":
        conf_line = f"**Confidence: {conf}** {LOW_CONFIDENCE_TAG if conf == 'Low' else ''}".rstrip()
    else:
        conf_line = f"**Confidence: {conf}**"

    parts = [
        f"# {md(isin)}",
        "",
        f"{md(master.get('name') or row['name'])} · {label} · Settlement {run.settlement} · "
        f"Run `{md(run.run_id)}`",
        "",
        "[Back to the report](../README.md)",
        "",
        f"> **Research classification only — not investment advice.** "
        f"Phase 1: eligibility screening only.",
        "",
        conf_line,
        "",
        "## Key facts",
        "",
        _table(["Field", "Value"], [[k, v] for k, v in facts]),
        "",
        "## Instrument ratings",
        "",
        "Instrument ratings only; issuer ratings are not used.",
        "",
        rating_block,
        "",
        "## Pricing",
        "",
        _table(["Field", "Value"], [[k, v] for k, v in pricing]),
        "",
        "## Availability (separate flags, not combined)",
        "",
        _table(["Flag", "Status"], [[k, v] for k, v in availability]),
        "",
        "## Why it passed",
        "",
        "\n".join(f"- {b}" for b in why) if why else "None recorded.",
        "",
        "## Concerns & flags",
        "",
        "\n".join(f"- {c}" for c in concerns) if concerns else "None flagged this run.",
        "",
        "## How principal is expected to be repaid",
        "",
        NOT_ASSESSED,
        "",
        "## Sources",
        "",
        _sources_table(detail),
        "",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- rejected.md

def _rejected_md(data: ReportData) -> str:
    run = data.run
    df = data.rejected
    total = len(df)
    parts = [
        f"# Rejected ISINs — {run.settlement}",
        "",
        f"Run `{md(run.run_id)}`. Rejected ISINs: {total}. "
        f"[Back to the report](README.md) · [rejected.csv](rejected.csv)",
        "",
        "## Count by rule",
        "",
        _rejected_summary(data.rule_counts),
        "",
        "## Per-ISIN reasons",
        "",
    ]
    if df.empty:
        parts.append(NONE_THIS_RUN)
    else:
        listing = df
        if total > REJECTED_CAP_TRIGGER:
            listing = df.head(REJECTED_CAP)
            parts.append(
                f"Listing capped to the first {REJECTED_CAP} of {total} rejected ISINs "
                f"(ordered by ISIN). The full list is in [rejected.csv](rejected.csv)."
            )
            parts.append("")
        rows = [[md(r["isin"]), md(r["name"]), md(r["rules"]), md(r["reasons"])]
                for _, r in listing.iterrows()]
        parts.append(_table(["ISIN", "Name", "Rules", "Reasons"], rows))
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------- CSV

def _csv_records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    """Flat CSV records. Missing values are empty cells; percent and price columns are 2dp."""
    records = []
    for _, r in df.iterrows():
        rec = {}
        for col in columns:
            value = r.get(col)
            if col in PERCENT_CSV_COLUMNS:
                rec[col] = csv_num(value)
            elif col == "rank" or col in ("executable_verified", "offered_for_sale"):
                rec[col] = "" if is_missing(value) else str(int(value))
            else:
                rec[col] = "" if is_missing(value) else str(value)
        records.append(rec)
    return records


def _write_csv(path: Path, columns: list[str], records: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)


def _shortlist_csv_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Rename dashboard columns to CSV names: our YTM and vendor YTM stay in separate columns."""
    return df.rename(columns={"ytm_pct": "our_ytm_pct", "vendor_ytm": "vendor_ytm_pct",
                              "nse_reported_yield": "nse_reported_yield_pct"})


# ---------------------------------------------------------------- summary, index

def _summary(data: ReportData) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "date": data.run.settlement,
        "run_id": data.run.run_id,
        "settlement_from": data.run.settlement_from,
        "counts": dict(sorted(data.counts.items())),
        "latest_price_date": data.latest_price_date,
    }


def rebuild_index(out_dir) -> Path:
    """Rewrite <out>/INDEX.md from every day folder's summary.json, newest day first."""
    out_dir = Path(out_dir)
    days = []
    for child in (out_dir.iterdir() if out_dir.is_dir() else []):
        if not (child.is_dir() and DAY_DIR_RE.match(child.name)):
            continue
        summary = child / SUMMARY_NAME
        if not summary.is_file():
            continue
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        days.append((child.name, data))
    days.sort(key=lambda item: item[0], reverse=True)

    parts = [
        "# Bond screener reports — index",
        "",
        "Newest settlement first. Each day links to its README.",
        "",
        f"> **{DISCLAIMER}**",
        "",
    ]
    if not days:
        parts.append("No reports yet.")
    else:
        rows = []
        for day, data in days:
            c = data.get("counts", {})
            rows.append([
                f"[{day}]({day}/README.md)",
                f"`{md(data.get('run_id'))}`",
                str(c.get("primary", DASH)),
                str(c.get("speculative", DASH)),
                str(c.get("rejected", DASH)),
            ])
        parts.append(_table(["Settlement date", "Run ID", "Primary", "Speculative (A+/A)", "Rejected"],
                            rows))
    parts.append("")
    index_path = out_dir / INDEX_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(parts), encoding="utf-8")
    return index_path


# ---------------------------------------------------------------- entry point

def write_report(conn, out_dir, run_id: str | None = None, generated_at: str | None = None) -> ReportResult:
    """Write <out_dir>/<settlement>/ for one run and refresh <out_dir>/INDEX.md.

    Re-running for the same settlement overwrites this folder's generated files. Stale bond
    pages from an earlier run in the same folder are removed. Read-only on the database.
    """
    run = resolve_run(conn, run_id)
    data = collect(conn, run)
    generated = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    day_dir = Path(out_dir) / run.settlement
    bonds_dir = day_dir / BONDS_DIR
    bonds_dir.mkdir(parents=True, exist_ok=True)
    for stale in bonds_dir.glob("*.md"):
        stale.unlink()

    written: list[str] = []

    def put(name: str, text: str) -> None:
        (day_dir / name).write_text(text, encoding="utf-8")
        written.append(name)

    put("README.md", _readme(data, generated))
    put("rejected.md", _rejected_md(data))

    for name, df in (("shortlist.csv", data.primary), ("speculative.csv", data.speculative)):
        records = _csv_records(_shortlist_csv_frame(df), SHORTLIST_CSV_COLUMNS)
        _write_csv(day_dir / name, SHORTLIST_CSV_COLUMNS, records)
        written.append(name)
    _write_csv(day_dir / "rejected.csv", REJECTED_CSV_COLUMNS,
               _csv_records(data.rejected, REJECTED_CSV_COLUMNS))
    written.append("rejected.csv")

    for bucket, df in (("primary", data.primary), ("speculative", data.speculative)):
        for _, row in df.iterrows():
            isin = row["isin"]
            detail = data.details.get(isin, {})
            put(f"{BONDS_DIR}/{isin}.md", render_bond_page(row, detail, run, bucket))

    put(SUMMARY_NAME, json.dumps(_summary(data), sort_keys=True, indent=2) + "\n")

    index_path = rebuild_index(out_dir)
    return ReportResult(
        run_id=run.run_id,
        settlement=run.settlement,
        day_dir=day_dir,
        files=tuple(written),
        index_path=index_path,
    )
