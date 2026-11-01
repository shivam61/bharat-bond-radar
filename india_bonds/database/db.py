"""SQLite connection and low-level write helpers for india-bonds.

Helpers do not commit; callers own transaction boundaries.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_INSTRUMENT_COLUMNS = {
    "issuer_id", "name", "exchange_listed", "instrument_type", "face_value",
    "coupon_pct", "coupon_frequency", "first_interest_date", "maturity_date",
    "issue_date", "security_type", "seniority", "collateral_text",
    "is_perpetual", "is_at1", "is_structured", "default_flag",
    "trading_suspended", "currency", "coupon_hint", "master_source_id",
    "source_url", "retrieved_on",
}


# Columns added after the first schema version. An existing DB created before them is
# migrated with ALTER TABLE ... ADD COLUMN (see _migrate_price_snapshot).
_PRICE_SNAPSHOT_ADDED_COLUMNS = (
    ("price_per_100", "REAL"),
    ("reported_yield_pct", "REAL"),
)


class SchemaRebuildRequired(RuntimeError):
    """An existing DB has a price_snapshot.kind CHECK without 'exchange_reported'.

    SQLite cannot alter a CHECK constraint in place, so such a DB must be rebuilt
    (delete the DB file and re-run refresh; raw files and CSV inputs are kept on disk).
    """


def connect(path) -> sqlite3.Connection:
    """Open a SQLite connection with Row results and foreign keys enforced."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes (idempotent), then apply the in-place column migration."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate_price_snapshot(conn)


def _migrate_price_snapshot(conn: sqlite3.Connection) -> None:
    """Add columns missing from an older price_snapshot table. Never alters the kind CHECK.

    CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so new columns are
    added here. An old table whose CHECK lacks 'exchange_reported' is detected at insert
    time (insert_price_snapshot raises SchemaRebuildRequired).
    """
    present = {row[1] for row in conn.execute("PRAGMA table_info(price_snapshot)")}
    for name, ddl in _PRICE_SNAPSHOT_ADDED_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE price_snapshot ADD COLUMN {name} {ddl}")
    conn.commit()


def insert_source_record(
    conn: sqlite3.Connection,
    *,
    source: str,
    url: str | None,
    fetched_at: str,
    content_type: str | None,
    size_bytes: int | None,
    sha256: str | None,
    raw_path: str | None,
    status: str,
    note: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO source_record (source, url, fetched_at, content_type, size_bytes, "
        "sha256, raw_path, status, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, url, fetched_at, content_type, size_bytes, sha256, raw_path, status, note),
    )
    return int(cur.lastrowid)


def upsert_issuer(
    conn: sqlite3.Connection,
    name: str,
    sector: str | None = None,
    group_name: str | None = None,
) -> int:
    """Return issuer id for name, creating it if needed. Non-null sector/group fill in, never blank out."""
    conn.execute(
        "INSERT INTO issuer (name, sector, group_name) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "sector = COALESCE(excluded.sector, issuer.sector), "
        "group_name = COALESCE(excluded.group_name, issuer.group_name)",
        (name, sector, group_name),
    )
    row = conn.execute("SELECT id FROM issuer WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def upsert_instrument(conn: sqlite3.Connection, isin: str, **fields) -> None:
    """Insert or update instrument by isin. Only the keyword fields passed are written."""
    unknown = set(fields) - _INSTRUMENT_COLUMNS
    if unknown:
        raise ValueError(f"unknown instrument columns: {sorted(unknown)}")
    cols = ["isin", *fields]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields) or "isin = excluded.isin"
    conn.execute(
        f"INSERT INTO instrument ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(isin) DO UPDATE SET {updates}",
        [isin, *fields.values()],
    )


def instrument_exists(conn: sqlite3.Connection, isin: str) -> bool:
    return conn.execute("SELECT 1 FROM instrument WHERE isin = ?", (isin,)).fetchone() is not None


def rating_exists(conn, *, isin, agency, action_date, rating) -> bool:
    return conn.execute(
        "SELECT 1 FROM rating WHERE isin = ? AND agency = ? AND action_date IS ? AND rating = ?",
        (isin, agency, action_date, rating),
    ).fetchone() is not None


def insert_rating(
    conn: sqlite3.Connection,
    *,
    isin: str,
    agency: str,
    rating: str,
    outlook: str | None,
    action: str | None,
    action_date: str | None,
    rationale_url: str | None,
    source_id: int | None,
    source_type: str | None = None,
    source_ref: str | None = None,
) -> bool:
    """Insert a rating. Returns False (no write) if the UNIQUE key already exists."""
    if rating_exists(conn, isin=isin, agency=agency, action_date=action_date, rating=rating):
        return False
    conn.execute(
        "INSERT INTO rating (isin, agency, rating, outlook, action, action_date, rationale_url, "
        "source_id, source_type, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (isin, agency, rating, outlook, action, action_date, rationale_url, source_id,
         source_type, source_ref),
    )
    return True


def price_snapshot_exists(
    conn, *, isin, kind, platform, observed_at, clean_price, vendor_ytm
) -> bool:
    return conn.execute(
        "SELECT 1 FROM price_snapshot WHERE isin = ? AND kind = ? AND platform IS ? "
        "AND observed_at IS ? AND clean_price IS ? AND vendor_ytm IS ?",
        (isin, kind, platform, observed_at, clean_price, vendor_ytm),
    ).fetchone() is not None


def insert_price_snapshot(
    conn: sqlite3.Connection,
    *,
    isin: str,
    trade_date: str | None,
    clean_price: float | None,
    volume: float | None,
    value: float | None,
    trades: int | None,
    kind: str,
    platform: str | None,
    vendor_ytm: float | None,
    freshness: str | None,
    source_id: int | None,
    observed_at: str | None,
    source_url: str | None = None,
    notes: str | None = None,
    price_per_100: float | None = None,
    reported_yield_pct: float | None = None,
) -> int:
    """Insert a price snapshot. Does not commit.

    kind='exchange_reported' rows carry price_per_100 (NSE price per 100 face, basis
    unverified) and reported_yield_pct; clean_price stays NULL for them.
    """
    params = (isin, trade_date, clean_price, volume, value, trades, kind, platform,
              vendor_ytm, freshness, source_id, observed_at, source_url, notes,
              price_per_100, reported_yield_pct)
    try:
        cur = conn.execute(
            "INSERT INTO price_snapshot (isin, trade_date, clean_price, volume, value, trades, kind, "
            "platform, vendor_ytm, freshness, source_id, observed_at, source_url, notes, "
            "price_per_100, reported_yield_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params,
        )
    except sqlite3.IntegrityError as exc:
        if kind == "exchange_reported" and "CHECK constraint" in str(exc):
            raise SchemaRebuildRequired(
                "the database's price_snapshot table was created before 'exchange_reported' was "
                "allowed and its kind CHECK cannot be altered in place. Delete the database file "
                "(for data/bonds.db: remove it, keep data/raw and data/inputs) and re-run refresh "
                f"to rebuild it. Original error: {exc}"
            ) from exc
        raise
    return int(cur.lastrowid)


def availability_exists(
    conn, *, isin, platform, observed_at, offered_for_sale, min_investment
) -> bool:
    return conn.execute(
        "SELECT 1 FROM availability WHERE isin = ? AND platform IS ? AND observed_at IS ? "
        "AND offered_for_sale IS ? AND min_investment IS ?",
        (isin, platform, observed_at, offered_for_sale, min_investment),
    ).fetchone() is not None


def insert_availability(
    conn: sqlite3.Connection,
    *,
    isin: str,
    listed: int | None,
    recently_traded: int | None,
    offered_for_sale: int | None,
    price_verified: int | None,
    executable_verified: int | None,
    min_investment: float | None,
    platform: str | None,
    observed_at: str | None,
    source_id: int | None,
    source_url: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO availability (isin, listed, recently_traded, offered_for_sale, price_verified, "
        "executable_verified, min_investment, platform, observed_at, source_id, source_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (isin, listed, recently_traded, offered_for_sale, price_verified,
         executable_verified, min_investment, platform, observed_at, source_id, source_url),
    )
    return int(cur.lastrowid)
