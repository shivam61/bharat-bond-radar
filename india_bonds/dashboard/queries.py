"""Pure SQLite/pandas queries for the research dashboard.

No streamlit imports here, so everything is unit-testable against a tmp DB.
All reads are read-only. Missing tables or columns yield empty frames, never
exceptions, so the dashboard can show a friendly message instead of crashing.

Research classification only. Nothing here computes a score or recommendation.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import pandas as pd

from india_bonds.screening.eligibility import RATING_SCALE

DB_ENV_VAR = "INDIA_BONDS_DB"
DEFAULT_DB_PATH = "data/bonds.db"

RATING_ORDER = {r: i for i, r in enumerate(RATING_SCALE)}
PAYOUT_FREQUENCIES = ("monthly", "quarterly", "semiannual", "annual", "zero", "at_maturity")
INCOME_FREQUENCIES = ("monthly", "quarterly")
CONFIDENCE_LEVELS = ("Low", "Medium", "High")
LOW_CONFIDENCE_MARKER = "LOW CONFIDENCE"

_RUN_SETTLEMENT = re.compile(r"screen-(\d{4}-\d{2}-\d{2})$")

# Price kinds shown with a plain-language label. 'exchange_reported' is the NSE OTC/RFQ feed:
# prices per 100 face, clean-price basis unverified.
PRICE_KIND_LABELS = {"exchange_reported": "NSE OTC/RFQ reported (basis unverified)"}
NSE_REPORTED_YIELD_LABEL = "NSE reported traded yield (unverified)"


def price_kind_label(kind):
    return PRICE_KIND_LABELS.get(kind, kind) if isinstance(kind, str) else kind


# (dataframe column, display label) in display order.
DISPLAY_COLUMNS: list[tuple[str, str]] = [
    ("rank", "Rank"),
    ("issuer", "Issuer"),
    ("isin", "ISIN"),
    ("name", "Name"),
    ("worst_rating", "Lowest instrument rating"),
    ("coupon_pct", "Coupon (%)"),
    ("ytm_pct", "Our YTM (%)"),
    ("vendor_ytm", "Vendor YTM (unverified)"),
    ("nse_reported_yield", NSE_REPORTED_YIELD_LABEL),
    ("clean_price", "Clean price"),
    ("dirty_price", "Dirty price"),
    ("discount_pct", "Discount (%)"),
    ("maturity_date", "Maturity date"),
    ("coupon_frequency", "Payout frequency"),
    ("next_interest_date", "Next interest date"),
    ("security_type", "Security type"),
    ("flags", "Flags"),
    ("confidence", "Confidence"),
    ("low_confidence", "Low confidence"),
    ("price_date", "Price date"),
    ("price_kind", "Price kind"),
    ("min_investment", "Min investment"),
    ("executable_verified", "Executable verified"),
    ("offered_for_sale", "Offered for sale"),
]

# Shortlist query columns (snake_case). ytm_pct is ytm * 100 (screen_result.ytm is a fraction).
SHORTLIST_COLUMNS = [
    "run_id", "isin", "bucket", "issuer", "name", "worst_rating", "coupon_pct", "coupon_frequency",
    "maturity_date", "security_type", "ytm_pct", "vendor_ytm", "nse_reported_yield",
    "clean_price", "dirty_price",
    "discount_pct", "next_interest_date", "flags", "confidence", "price_date", "price_kind",
    "min_investment", "executable_verified", "offered_for_sale", "availability_observed_at",
]

REJECTED_COLUMNS = ["isin", "issuer", "name", "rules", "reasons", "rule_count"]
RULE_COUNT_COLUMNS = ["rule", "count"]


# ---------------------------------------------------------------- connection

def resolve_db_path(env: dict | None = None) -> Path:
    """DB path from INDIA_BONDS_DB, else data/bonds.db (relative to cwd)."""
    source = os.environ if env is None else env
    return Path(source.get(DB_ENV_VAR) or DEFAULT_DB_PATH)


def open_readonly(path) -> sqlite3.Connection | None:
    """Open an existing DB read-only. Returns None if the file is missing or unreadable.

    Never creates a file (plain sqlite3.connect would create an empty DB).
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn: sqlite3.Connection | None, name: str) -> bool:
    if conn is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _read(conn, sql: str, params=(), tables=(), columns=()) -> pd.DataFrame:
    """Run a read query; empty frame with `columns` when a table is missing or the query fails."""
    empty = pd.DataFrame(columns=list(columns))
    if conn is None or not all(has_table(conn, t) for t in tables):
        return empty
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return empty


# ---------------------------------------------------------------- runs

def settlement_from_run_id(run_id: str | None) -> str | None:
    """run_screen's default run_id is 'screen-<settlement>'. Custom run_ids carry no settlement."""
    if not run_id:
        return None
    m = _RUN_SETTLEMENT.search(run_id)
    return m.group(1) if m else None


def latest_run(conn) -> dict | None:
    """Most recent run by max(created_at) in screen_result.

    Returns {'run_id', 'created_at', 'settlement'} or None when there is no run / no table.
    settlement is parsed from the run_id and is None when the run_id is custom.
    """
    df = _read(
        conn,
        "SELECT run_id, MAX(created_at) AS created_at FROM screen_result "
        "WHERE run_id IS NOT NULL GROUP BY run_id ORDER BY created_at DESC, run_id DESC LIMIT 1",
        tables=("screen_result",),
        columns=("run_id", "created_at"),
    )
    if df.empty:
        return None
    run_id = str(df.iloc[0]["run_id"])
    created = df.iloc[0]["created_at"]
    return {
        "run_id": run_id,
        "created_at": None if pd.isna(created) else str(created),
        "settlement": settlement_from_run_id(run_id),
    }


def all_isins(conn, run_id: str) -> list[str]:
    df = _read(
        conn,
        "SELECT DISTINCT isin FROM screen_result WHERE run_id = ? ORDER BY isin",
        (run_id,),
        tables=("screen_result",),
        columns=("isin",),
    )
    return df["isin"].tolist()


# ---------------------------------------------------------------- shortlists

def _latest_availability_sql() -> str:
    # Latest availability row per ISIN: observed_at desc, then id desc.
    return (
        "SELECT a.* FROM availability a WHERE a.id = ("
        "  SELECT a2.id FROM availability a2 WHERE a2.isin = a.isin"
        "  ORDER BY a2.observed_at DESC, a2.id DESC LIMIT 1)"
    )


def shortlist(conn, run_id: str, bucket: str) -> pd.DataFrame:
    """screen_result rows for one bucket ('primary' or 'speculative') with instrument, issuer and
    latest availability joined in. Sorted by our YTM desc (rank is assigned later by add_rank).

    Vendor YTM is returned in its own column and is never used as our YTM. For exchange_reported
    rows the stored vendor_ytm slot holds NSE's reported traded yield; it is returned as
    nse_reported_yield (never as vendor_ytm) and is never used as our YTM.
    Availability flags are returned as separate columns and are not combined.
    """
    sql = f"""
        SELECT s.run_id, s.isin, s.bucket,
               iss.name AS issuer, i.name AS name, s.worst_rating,
               i.coupon_pct, i.coupon_frequency, i.maturity_date, i.security_type,
               s.ytm * 100.0 AS ytm_pct,
               CASE WHEN s.price_kind = 'exchange_reported' THEN NULL ELSE s.vendor_ytm END AS vendor_ytm,
               CASE WHEN s.price_kind = 'exchange_reported' THEN s.vendor_ytm END AS nse_reported_yield,
               s.clean_price, s.dirty_price, s.discount_pct, s.next_interest_date,
               s.flags, s.confidence, s.price_date, s.price_kind,
               av.min_investment, av.executable_verified, av.offered_for_sale,
               av.observed_at AS availability_observed_at
        FROM screen_result s
        LEFT JOIN instrument i ON i.isin = s.isin
        LEFT JOIN issuer iss ON iss.id = i.issuer_id
        LEFT JOIN ({_latest_availability_sql()}) av ON av.isin = s.isin
        WHERE s.run_id = ? AND s.bucket = ?
        ORDER BY s.ytm DESC, s.isin
    """
    df = _read(
        conn, sql, (run_id, bucket),
        tables=("screen_result", "instrument", "issuer", "availability"),
        columns=SHORTLIST_COLUMNS,
    )
    return _with_derived(df)


def _with_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived display columns without changing the stored values."""
    df = df.copy()
    if df.empty:
        for col in SHORTLIST_COLUMNS:
            df[col] = pd.Series(dtype=object)
    df["low_confidence"] = df["confidence"].map(
        lambda c: LOW_CONFIDENCE_MARKER if c == "Low" else ""
    )
    # Discount threshold flags, same cut-offs as analytics.ytm (strict '>').
    disc = pd.to_numeric(df["discount_pct"], errors="coerce")
    df["below_98"] = disc > 2
    df["below_95"] = disc > 5
    df["below_90"] = disc > 10
    return df


def add_rank(df: pd.DataFrame) -> pd.DataFrame:
    """Sort by our YTM desc and number rows 1..n within the frame passed in (call after filtering)."""
    df = df.copy()
    if df.empty:
        df.insert(0, "rank", pd.Series(dtype="Int64"))
        return df
    df = df.sort_values(["ytm_pct", "isin"], ascending=[False, True], na_position="last")
    df.insert(0, "rank", range(1, len(df) + 1))
    return df.reset_index(drop=True)


def discounted(conn, run_id: str) -> pd.DataFrame:
    """Primary and speculative rows with discount_pct > 2, with bucket kept as a column."""
    frames = [shortlist(conn, run_id, b) for b in ("primary", "speculative")]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return df
    disc = pd.to_numeric(df["discount_pct"], errors="coerce")
    return df[disc > 2].sort_values("discount_pct", ascending=False).reset_index(drop=True)


def income(conn, run_id: str) -> pd.DataFrame:
    """Primary and speculative rows whose coupon_frequency is monthly or quarterly.

    Bucket is kept as a column so the reader can see which bucket each bond is in.
    """
    frames = [shortlist(conn, run_id, b) for b in ("primary", "speculative")]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return df
    return df[df["coupon_frequency"].isin(INCOME_FREQUENCIES)].reset_index(drop=True)


# ---------------------------------------------------------------- rejections

def rejected(conn, run_id: str) -> pd.DataFrame:
    """One row per rejected ISIN: all rules and reasons for that ISIN in the run."""
    df = _read(
        conn,
        "SELECT r.isin, r.rule, r.reason, iss.name AS issuer, i.name AS name "
        "FROM rejection r LEFT JOIN instrument i ON i.isin = r.isin "
        "LEFT JOIN issuer iss ON iss.id = i.issuer_id "
        "WHERE r.run_id = ? ORDER BY r.isin, r.rule",
        (run_id,),
        tables=("rejection",),
        columns=("isin", "rule", "reason", "issuer", "name"),
    )
    if df.empty:
        return pd.DataFrame(columns=REJECTED_COLUMNS)
    grouped = (
        df.groupby("isin", sort=True)
        .agg(
            issuer=("issuer", "first"),
            name=("name", "first"),
            rules=("rule", lambda s: "; ".join(dict.fromkeys(s))),
            reasons=("reason", lambda s: " | ".join(dict.fromkeys(str(x) for x in s if x))),
            rule_count=("rule", "nunique"),
        )
        .reset_index()
    )
    return grouped[REJECTED_COLUMNS]


def rejection_counts(conn, run_id: str) -> pd.DataFrame:
    """Number of distinct rejected ISINs per rule, most frequent first (for the bar chart)."""
    df = _read(
        conn,
        "SELECT rule, COUNT(DISTINCT isin) AS count FROM rejection WHERE run_id = ? "
        "GROUP BY rule ORDER BY count DESC, rule",
        (run_id,),
        tables=("rejection",),
        columns=RULE_COUNT_COLUMNS,
    )
    return df[RULE_COUNT_COLUMNS]


# ---------------------------------------------------------------- filters

def apply_filters(
    df: pd.DataFrame,
    *,
    ratings=None,
    ytm_range: tuple[float, float] | None = None,
    maturity_before=None,
    frequencies=None,
    issuer_query: str | None = None,
    min_confidence: str | None = None,
    discount_min: float | None = None,
) -> pd.DataFrame:
    """Apply sidebar filters. None or empty values mean 'no filter'.

    ratings: iterable of worst_rating values to keep.
    ytm_range: (min, max) in percent, compared against ytm_pct (inclusive).
    maturity_before: keep rows whose maturity_date is strictly before this date.
    frequencies: iterable of coupon_frequency values.
    issuer_query: case-insensitive substring match on issuer or name.
    min_confidence: 'Low' | 'Medium' | 'High'; keeps that level and above.
    discount_min: keep rows with discount_pct >= this value.
    """
    if df is None or df.empty:
        return df
    out = df
    if ratings:
        out = out[out["worst_rating"].isin(list(ratings))]
    if ytm_range is not None:
        lo, hi = ytm_range
        y = pd.to_numeric(out["ytm_pct"], errors="coerce")
        out = out[(y >= lo) & (y <= hi)]
    if maturity_before is not None:
        mat = pd.to_datetime(out["maturity_date"], errors="coerce")
        cutoff = pd.Timestamp(maturity_before)
        out = out[mat < cutoff]
    if frequencies:
        out = out[out["coupon_frequency"].isin(list(frequencies))]
    if issuer_query:
        q = issuer_query.strip().lower()
        if q:
            hay = out["issuer"].fillna("").str.lower() + " " + out["name"].fillna("").str.lower()
            out = out[hay.str.contains(q, regex=False)]
    if min_confidence:
        order = {c: i for i, c in enumerate(CONFIDENCE_LEVELS)}
        threshold = order.get(min_confidence)
        if threshold is not None:
            out = out[out["confidence"].map(lambda c: order.get(c, -1)) >= threshold]
    if discount_min is not None:
        disc = pd.to_numeric(out["discount_pct"], errors="coerce")
        out = out[disc >= discount_min]
    return out.reset_index(drop=True)


def ytm_bounds(df: pd.DataFrame) -> tuple[float, float] | None:
    """Floor/ceil of ytm_pct for slider defaults, or None when no YTM values exist."""
    if df is None or df.empty:
        return None
    y = pd.to_numeric(df["ytm_pct"], errors="coerce").dropna()
    if y.empty:
        return None
    return float(y.min()), float(y.max())


BUCKET_LABELS = {"primary": "Primary", "speculative": "Speculative (A+/A)"}
DISCOUNT_FLAG_COLUMNS: list[tuple[str, str]] = [
    ("below_98", "Below 98"), ("below_95", "Below 95"), ("below_90", "Below 90"),
]


def display_frame(df: pd.DataFrame, *, bucket: bool = False, discount_flags: bool = False) -> pd.DataFrame:
    """Select and rename columns for display, in DISPLAY_COLUMNS order.

    bucket=True prepends a 'Bucket' column (Primary / Speculative) for mixed-bucket tabs.
    discount_flags=True appends Yes/No columns for the 98/95/90 discount thresholds.
    Availability flags become 'Yes' / 'No' / 'Unknown' text, one column each.
    """
    if df is None:
        df = pd.DataFrame()
    out = df.copy()
    columns = list(DISPLAY_COLUMNS)
    for col, _ in columns:
        if col not in out.columns:
            out[col] = None
    for col in ("executable_verified", "offered_for_sale"):
        out[col] = out[col].map(flag_text)
    labels = dict(columns)
    keys = [c for c, _ in columns]
    if bucket:
        out["bucket"] = out.get("bucket", pd.Series(dtype=object)).map(BUCKET_LABELS)
        keys = ["bucket", *keys]
        labels["bucket"] = "Bucket"
    if discount_flags:
        for col, label in DISCOUNT_FLAG_COLUMNS:
            out[col] = out.get(col, pd.Series(dtype=object)).map(
                lambda v: "Yes" if v is True or v == 1 else "No"
            )
            keys.append(col)
            labels[col] = label
    if "price_kind" in out.columns:
        out["price_kind"] = out["price_kind"].map(price_kind_label)
    out = out[keys]
    return out.rename(columns=labels)


def flag_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unknown"
    try:
        return "Yes" if int(value) == 1 else "No" if int(value) == 0 else "Unknown"
    except (TypeError, ValueError):
        return "Unknown"


# ---------------------------------------------------------------- detail

def isin_detail(conn, isin: str, run_id: str | None = None) -> dict:
    """Everything shown in the per-ISIN expander. Missing pieces come back empty, never raise."""
    master = _read(
        conn,
        "SELECT i.*, iss.name AS issuer_name FROM instrument i "
        "LEFT JOIN issuer iss ON iss.id = i.issuer_id WHERE i.isin = ?",
        (isin,),
        tables=("instrument", "issuer"),
    )
    ratings = _read(
        conn,
        "SELECT agency, rating, outlook, action, action_date, source_type, source_ref, "
        "rationale_url, source_id FROM rating WHERE isin = ? ORDER BY action_date, id",
        (isin,),
        tables=("rating",),
        columns=("agency", "rating", "outlook", "action", "action_date", "source_type",
                 "source_ref", "rationale_url", "source_id"),
    )
    prices = _read(
        conn,
        "SELECT trade_date, observed_at, clean_price, price_per_100, vendor_ytm, reported_yield_pct, "
        "kind, platform, freshness, source_url, source_id FROM price_snapshot WHERE isin = ? "
        "ORDER BY COALESCE(trade_date, observed_at) DESC, id DESC",
        (isin,),
        tables=("price_snapshot",),
        columns=("trade_date", "observed_at", "clean_price", "price_per_100", "vendor_ytm",
                 "reported_yield_pct", "kind", "platform", "freshness", "source_url", "source_id"),
    )
    avail = _read(
        conn,
        "SELECT listed, recently_traded, offered_for_sale, price_verified, executable_verified, "
        "min_investment, platform, observed_at, source_url, source_id FROM availability "
        "WHERE isin = ? ORDER BY observed_at DESC, id DESC",
        (isin,),
        tables=("availability",),
        columns=("listed", "recently_traded", "offered_for_sale", "price_verified",
                 "executable_verified", "min_investment", "platform", "observed_at",
                 "source_url", "source_id"),
    )
    if run_id is not None:
        rej = _read(
            conn,
            "SELECT rule, reason, created_at FROM rejection WHERE isin = ? AND run_id = ? "
            "ORDER BY rule",
            (isin, run_id),
            tables=("rejection",),
            columns=("rule", "reason", "created_at"),
        )
    else:
        rej = pd.DataFrame(columns=["rule", "reason", "created_at"])
    sources = _source_urls(conn, isin)
    return {
        "master": master.iloc[0].to_dict() if not master.empty else None,
        "ratings": ratings,
        "prices": prices,
        "availability": avail,
        "rejections": rej,
        "sources": sources,
    }


def _source_urls(conn, isin: str) -> pd.DataFrame:
    """Every URL and timestamp recorded for an ISIN: instrument, ratings, prices, availability."""
    cols = ["origin", "url", "fetched_at", "status"]
    rows: list[dict] = []

    def add(origin, url, fetched, status=None):
        if url or fetched:
            rows.append({"origin": origin, "url": url, "fetched_at": fetched, "status": status})

    master = _read(conn, "SELECT source_url, retrieved_on, master_source_id FROM instrument "
                   "WHERE isin = ?", (isin,), tables=("instrument",),
                   columns=("source_url", "retrieved_on", "master_source_id"))
    if not master.empty:
        m = master.iloc[0]
        add("instrument master", m["source_url"], m["retrieved_on"])
    src = _read(
        conn,
        "SELECT sr.url, sr.fetched_at, sr.status FROM instrument i "
        "JOIN source_record sr ON sr.id = i.master_source_id WHERE i.isin = ?",
        (isin,), tables=("instrument", "source_record"), columns=("url", "fetched_at", "status"),
    )
    for _, r in src.iterrows():
        add("instrument master (source_record)", r["url"], r["fetched_at"], r["status"])
    rat = _read(
        conn,
        "SELECT r.agency, r.rationale_url, r.action_date, sr.url AS src_url, sr.fetched_at, "
        "sr.status FROM rating r LEFT JOIN source_record sr ON sr.id = r.source_id WHERE r.isin = ?",
        (isin,), tables=("rating", "source_record"),
        columns=("agency", "rationale_url", "action_date", "src_url", "fetched_at", "status"),
    )
    for _, r in rat.iterrows():
        add(f"rating {r['agency']}", r["rationale_url"], r["action_date"])
        add(f"rating {r['agency']} (source_record)", r["src_url"], r["fetched_at"], r["status"])
    pr = _read(
        conn,
        "SELECT p.kind, p.source_url, p.observed_at, sr.url AS src_url, sr.fetched_at, sr.status "
        "FROM price_snapshot p LEFT JOIN source_record sr ON sr.id = p.source_id WHERE p.isin = ?",
        (isin,), tables=("price_snapshot", "source_record"),
        columns=("kind", "source_url", "observed_at", "src_url", "fetched_at", "status"),
    )
    for _, r in pr.iterrows():
        add(f"price {r['kind']}", r["source_url"], r["observed_at"])
        add(f"price {r['kind']} (source_record)", r["src_url"], r["fetched_at"], r["status"])
    av = _read(
        conn,
        "SELECT a.source_url, a.observed_at, sr.url AS src_url, sr.fetched_at, sr.status "
        "FROM availability a LEFT JOIN source_record sr ON sr.id = a.source_id WHERE a.isin = ?",
        (isin,), tables=("availability", "source_record"),
        columns=("source_url", "observed_at", "src_url", "fetched_at", "status"),
    )
    for _, r in av.iterrows():
        add("availability", r["source_url"], r["observed_at"])
        add("availability (source_record)", r["src_url"], r["fetched_at"], r["status"])
    return pd.DataFrame(rows, columns=cols)
