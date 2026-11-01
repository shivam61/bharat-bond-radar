"""Manual CSV imports: instrument master, ratings, marketplace prices.

Each import:
  - requires an exact header match (otherwise the whole file is rejected),
  - validates row by row; bad rows are reported, not written,
  - writes one source_record row per file (sha256 of the file bytes),
  - never fills in missing values: blank cells become NULL.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from india_bonds.database import db

ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

MASTER_HEADER = [
    "isin", "issuer_name", "instrument_name", "face_value", "coupon_pct",
    "coupon_frequency", "first_interest_date", "maturity_date", "issue_date",
    "security_type", "seniority", "collateral_text", "is_perpetual", "is_at1",
    "is_structured", "default_flag", "trading_suspended", "currency",
    "source_url", "retrieved_on",
]
RATINGS_HEADER = [
    "isin", "issuer_name", "instrument_name", "instrument_type", "rating_agency",
    "rating", "outlook", "rating_action", "action_date", "rationale_url",
    "source_type", "source_ref", "retrieved_on",
]
MARKETPLACE_HEADER = [
    "isin", "platform", "instrument_name", "issuer_name", "face_value", "price",
    "ytm_pct", "coupon_pct", "coupon_frequency", "maturity_date", "min_investment",
    "availability", "security_type", "collateral_text", "observed_at", "source_url",
    "notes",
]

COUPON_FREQUENCIES = {"monthly", "quarterly", "semiannual", "annual", "zero", "at_maturity"}
SECURITY_TYPES = {"secured", "unsecured", "unknown"}
SENIORITIES = {"senior", "subordinated", "unknown"}
RATING_ACTIONS = {"assigned", "reaffirmed", "upgraded", "downgraded", "withdrawn"}
SOURCE_TYPES = {"nse_filing", "bse_filing", "agency_site", "manual"}
AVAILABILITIES = {"open", "closed", "sold_out", "unknown"}

MASTER_BOOL_FIELDS = (
    "is_perpetual", "is_at1", "is_structured", "default_flag", "trading_suspended",
)


@dataclass
class ImportReport:
    rows_read: int = 0
    rows_ok: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    source_record_id: int | None = None
    duplicates: int = 0  # rows skipped because the same record already exists (not an error)


class _RowError(ValueError):
    """A row-level validation failure; message is reported with the row number."""


# ---------- cell parsers (cells are already stripped; "" means blank) ----------

def _opt(cells: dict, key: str) -> str | None:
    return cells[key] or None


def _required(name: str, value: str) -> str:
    if not value:
        raise _RowError(f"{name} is blank")
    return value


def _req_isin(value: str) -> str:
    if not value:
        raise _RowError("isin is blank")
    if not ISIN_RE.fullmatch(value):
        raise _RowError(f"invalid isin {value!r} (expected ^IN[A-Z0-9]{{10}}$)")
    return value


def _num(name: str, value: str, *, minimum: float | None = None, positive: bool = False) -> float | None:
    if not value:
        return None
    try:
        x = float(value)
    except ValueError:
        raise _RowError(f"{name}: not a number {value!r}") from None
    if not math.isfinite(x):
        raise _RowError(f"{name}: not a finite number {value!r}")
    if positive and x <= 0:
        raise _RowError(f"{name}: must be > 0, got {value!r}")
    if minimum is not None and x < minimum:
        raise _RowError(f"{name}: must be >= {minimum}, got {value!r}")
    return x


def _timestamp(name: str, value: str, *, required: bool = False) -> str | None:
    """Accept YYYY-MM-DD or an ISO-8601 datetime (e.g. 2026-10-29T10:00:00Z); stored verbatim."""
    if value and "T" in value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise _RowError(f"{name}: invalid ISO datetime {value!r}") from None
        return value
    return _date(name, value, required=required)


def _date(name: str, value: str, *, required: bool = False) -> str | None:
    if not value:
        if required:
            raise _RowError(f"{name} is blank")
        return None
    if not DATE_RE.fullmatch(value):
        raise _RowError(f"{name}: not ISO YYYY-MM-DD {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise _RowError(f"{name}: invalid calendar date {value!r}") from None
    return value


def _enum(name: str, value: str, allowed: set[str], *, required: bool = False) -> str | None:
    if not value:
        if required:
            raise _RowError(f"{name} is blank")
        return None
    if value not in allowed:
        raise _RowError(f"{name}: {value!r} not in {sorted(allowed)}")
    return value


def _bool(name: str, value: str) -> int | None:
    if not value:
        return None
    if value == "true":
        return 1
    if value == "false":
        return 0
    raise _RowError(f"{name}: expected true/false/blank, got {value!r}")


def _currency(value: str) -> str | None:
    if not value:
        return None
    if not CURRENCY_RE.fullmatch(value):
        raise _RowError(f"currency: expected 3 uppercase letters, got {value!r}")
    return value


# ---------- row handlers: return True if something was written, False if duplicate ----------

def _master_row(conn: sqlite3.Connection, c: dict, source_id: int) -> bool:
    isin = _req_isin(c["isin"])
    face_value = _num("face_value", c["face_value"], positive=True)
    coupon_pct = _num("coupon_pct", c["coupon_pct"], minimum=0)
    coupon_frequency = _enum("coupon_frequency", c["coupon_frequency"], COUPON_FREQUENCIES)
    first_interest_date = _date("first_interest_date", c["first_interest_date"])
    maturity_date = _date("maturity_date", c["maturity_date"])
    issue_date = _date("issue_date", c["issue_date"])
    security_type = _enum("security_type", c["security_type"], SECURITY_TYPES)
    seniority = _enum("seniority", c["seniority"], SENIORITIES)
    bools = {f: _bool(f, c[f]) for f in MASTER_BOOL_FIELDS}
    _currency(c["currency"])  # validated; the currency value itself is stored below
    retrieved_on = _date("retrieved_on", c["retrieved_on"])

    issuer_name = _opt(c, "issuer_name")
    issuer_id = db.upsert_issuer(conn, issuer_name) if issuer_name else None
    db.upsert_instrument(
        conn,
        isin,
        issuer_id=issuer_id,
        name=_opt(c, "instrument_name"),
        face_value=face_value,
        coupon_pct=coupon_pct,
        coupon_frequency=coupon_frequency,
        first_interest_date=first_interest_date,
        maturity_date=maturity_date,
        issue_date=issue_date,
        security_type=security_type,
        seniority=seniority,
        collateral_text=_opt(c, "collateral_text"),
        currency=_opt(c, "currency"),
        master_source_id=source_id,
        source_url=_opt(c, "source_url"),
        retrieved_on=retrieved_on,
        **bools,
    )
    return True


def _ratings_row(conn: sqlite3.Connection, c: dict, source_id: int) -> bool:
    isin = _req_isin(c["isin"])
    agency = _required("rating_agency", c["rating_agency"])
    rating = _required("rating", c["rating"])
    action = _enum("rating_action", c["rating_action"], RATING_ACTIONS, required=True)
    action_date = _date("action_date", c["action_date"], required=True)
    source_type = _enum("source_type", c["source_type"], SOURCE_TYPES, required=True)
    _date("retrieved_on", c["retrieved_on"])
    if not db.instrument_exists(conn, isin):
        raise _RowError("unknown isin, import instrument master first")
    return db.insert_rating(
        conn,
        isin=isin,
        agency=agency,
        rating=rating,
        outlook=_opt(c, "outlook"),
        action=action,
        action_date=action_date,
        rationale_url=_opt(c, "rationale_url"),
        source_id=source_id,
        source_type=source_type,
        source_ref=_opt(c, "source_ref"),
    )


def _marketplace_row(conn: sqlite3.Connection, c: dict, source_id: int) -> bool:
    isin = _req_isin(c["isin"])
    platform = _required("platform", c["platform"])
    observed_at = _timestamp("observed_at", c["observed_at"], required=True)
    availability = _enum("availability", c["availability"], AVAILABILITIES)
    price = _num("price", c["price"], positive=True)
    ytm_pct = _num("ytm_pct", c["ytm_pct"])
    min_investment = _num("min_investment", c["min_investment"], minimum=0)
    # Validated for format only; instrument facts come from the master import, not vendors.
    _num("face_value", c["face_value"], positive=True)
    _num("coupon_pct", c["coupon_pct"], minimum=0)
    _enum("coupon_frequency", c["coupon_frequency"], COUPON_FREQUENCIES)
    _date("maturity_date", c["maturity_date"])
    _enum("security_type", c["security_type"], SECURITY_TYPES)
    if not db.instrument_exists(conn, isin):
        raise _RowError("unknown isin, import instrument master first")

    wrote = False
    if price is not None or ytm_pct is not None:
        if not db.price_snapshot_exists(
            conn, isin=isin, kind="marketplace", platform=platform,
            observed_at=observed_at, clean_price=price, vendor_ytm=ytm_pct,
        ):
            db.insert_price_snapshot(
                conn,
                isin=isin,
                trade_date=None,
                clean_price=price,
                volume=None,
                value=None,
                trades=None,
                kind="marketplace",
                platform=platform,
                vendor_ytm=ytm_pct,
                freshness="indicative",
                source_id=source_id,
                observed_at=observed_at,
                source_url=_opt(c, "source_url"),
                notes=_opt(c, "notes"),
            )
            wrote = True

    # Listing a row is not a verified purchase: price/executable verification stay 0.
    offered = None if availability is None else int(availability == "open")
    if not db.availability_exists(
        conn, isin=isin, platform=platform, observed_at=observed_at,
        offered_for_sale=offered, min_investment=min_investment,
    ):
        db.insert_availability(
            conn,
            isin=isin,
            listed=None,
            recently_traded=None,
            offered_for_sale=offered,
            price_verified=0,
            executable_verified=0,
            min_investment=min_investment,
            platform=platform,
            observed_at=observed_at,
            source_id=source_id,
            source_url=_opt(c, "source_url"),
        )
        wrote = True
    return wrote


# ---------- shared import driver ----------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def source_label(path) -> str:
    """Stable, non-identifying label for an imported file: project-relative path when the file is
    inside the project, else just the file name (never an absolute home path)."""
    p = Path(path).resolve()
    try:
        return p.relative_to(_PROJECT_ROOT).as_posix()
    except ValueError:
        return p.name


def _run_import(conn, path, source: str, header: list[str], handler) -> ImportReport:
    path = Path(path).resolve()
    label = source_label(path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    report = ImportReport()
    common = dict(
        source=source,
        url=label,
        fetched_at=_utc_now(),
        content_type="text/csv",
        size_bytes=len(raw),
        sha256=sha,
        raw_path=label,
    )

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        msg = "file is not valid UTF-8"
        report.source_record_id = db.insert_source_record(conn, **common, status="error", note=msg)
        conn.commit()
        report.errors.append((1, msg))
        return report

    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or rows[0] != header:
        got = rows[0] if rows else []
        msg = f"header mismatch: expected {header}, got {got}"
        report.source_record_id = db.insert_source_record(conn, **common, status="error", note=msg)
        conn.commit()
        report.errors.append((1, msg))
        return report

    report.source_record_id = db.insert_source_record(conn, **common, status="ok")
    try:
        for line_no, row in enumerate(rows[1:], start=2):
            if all(not cell.strip() for cell in row):
                continue  # fully blank line
            report.rows_read += 1
            if len(row) != len(header):
                report.errors.append((line_no, f"expected {len(header)} cells, got {len(row)}"))
                continue
            cells = {k: v.strip() for k, v in zip(header, row)}
            conn.execute("SAVEPOINT row_import")
            try:
                wrote = handler(conn, cells, report.source_record_id)
            except _RowError as exc:
                conn.execute("ROLLBACK TO row_import")
                conn.execute("RELEASE row_import")
                report.errors.append((line_no, str(exc)))
                continue
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK TO row_import")
                conn.execute("RELEASE row_import")
                report.errors.append((line_no, f"database error: {exc}"))
                continue
            conn.execute("RELEASE row_import")
            if wrote:
                report.rows_ok += 1
            else:
                report.duplicates += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return report


def import_instrument_master(conn: sqlite3.Connection, path) -> ImportReport:
    """Upsert instruments by ISIN from instrument_master.csv."""
    return _run_import(conn, path, "csv:instrument_master", MASTER_HEADER, _master_row)


def import_ratings(conn: sqlite3.Connection, path) -> ImportReport:
    """Insert instrument-level ratings from rating_import.csv. Never creates instruments."""
    return _run_import(conn, path, "csv:ratings", RATINGS_HEADER, _ratings_row)


def import_marketplace(conn: sqlite3.Connection, path) -> ImportReport:
    """Insert marketplace price snapshots and availability rows from marketplace_price_import.csv."""
    return _run_import(conn, path, "csv:marketplace", MARKETPLACE_HEADER, _marketplace_row)
