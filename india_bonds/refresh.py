"""Daily refresh: fetch sources, update the database only when data changed, screen, report.

Steps (one run):
  1. Create the DB file/dir if missing and apply the schema (idempotent).
  2. Unless offline: NSE debt universe, NSE CM bhavcopy (walk-back built in), NSE OTC/RFQ
     corporate bond trades (two JSON indices; skipped by --no-corp-trades), BSE stub.
     - A fetched file whose sha256 equals the latest record for the same source+url is
       'unchanged': nothing is ingested, and the duplicate raw file is deleted.
     - Changed bhavcopy file: a price row is written only when (isin, trade_date) is new
       or clean_price/volume differs from the stored row; an availability row only when
       its flags differ from the latest availability row for that ISIN.
     - Corp trades: per index file, sha256 vs its latest ok record. Changed indices are recorded
       and ingested in one transaction; unchanged indices are not recorded. A price row is written
       only when new or changed (see pipeline.ingest_nse_corp_trades). Warns when the newest
       response timestamp is more than CORP_TRADES_MAX_AGE_DAYS before the settlement date.
     - A failed source never aborts the run; it adds a warning and is recorded once per
       distinct outcome (repeats of the same unavailable note are not re-recorded).
  3. CSV inputs from the inputs folder: instrument_master.csv, ratings.csv,
     marketplace.csv (in that order). Missing files are 'not provided'; a file whose
     sha256 matches its latest record is 'unchanged' and not re-imported.
  4. Screen the settlement date (run_id 'screen-<settlement>'). Skipped when the run wrote
     nothing and a screen for that run_id already exists; the stored result is reused.
  5. Write the Markdown report folder unless disabled.

Cron example (weekdays 18:30 IST, appends to a log):
    30 18 * * 1-5 cd /path/india-bonds && .venv/bin/python scripts/refresh.py >> data/refresh.log 2>&1

Research classification only. Nothing here is investment advice.
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from india_bonds import pipeline
from india_bonds.data_sources import csv_import as ci
from india_bonds.data_sources.base import STATUS_OK, SourceRecord
from india_bonds.data_sources.bse import BseDebt
from india_bonds.data_sources.nse import (
    CORP_TRADES_MAX_AGE_DAYS,
    NseCmBhavcopy,
    NseCorpBondTrades,
    NseDebtUniverse,
    index_from_url,
)
from india_bonds.database import db
from india_bonds.enrichment.descriptor import parse_descriptor
from india_bonds.reports.markdown import NoRunError, write_report
from india_bonds.screening.eligibility import STALE_PRICE_DAYS
from india_bonds.screening.screener import run_screen

IST = timezone(timedelta(hours=5, minutes=30))
MAX_ERRORS_KEPT = 10
TOP_RULES = 5

# (file name in inputs dir, importer, exact header, csv_import source name)
CSV_INPUTS = (
    ("instrument_master.csv", ci.import_instrument_master, ci.MASTER_HEADER, "csv:instrument_master"),
    ("ratings.csv", ci.import_ratings, ci.RATINGS_HEADER, "csv:ratings"),
    ("marketplace.csv", ci.import_marketplace, ci.MARKETPLACE_HEADER, "csv:marketplace"),
)

NO_MASTER_WARNING = (
    "no instrument_master.csv: all bonds will be rejected for missing coupon/maturity data"
)
_PARSE_ERRORS = (ValueError, OSError, csv.Error, zipfile.BadZipFile)


class DatabaseOpenError(RuntimeError):
    """The database file/dir could not be created, opened, or initialised."""


@dataclass
class SourceStatus:
    name: str
    status: str  # ok | unavailable | error | skipped
    note: Optional[str] = None
    size_bytes: Optional[int] = None


@dataclass
class CsvSummary:
    name: str
    provided: bool
    unchanged_since: Optional[str] = None
    rows_read: int = 0
    rows_ok: int = 0
    duplicates: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)  # first MAX_ERRORS_KEPT only


@dataclass
class RefreshSummary:
    db_path: str
    settlement: str
    offline: bool
    sources: list[SourceStatus] = field(default_factory=list)
    db_writes: int = 0
    universe_rows: Optional[int] = None
    bhavcopy_trade_date: Optional[str] = None
    bhavcopy_walkback_days: int = 0
    bhavcopy_file_unchanged: bool = False
    bhav_new: Optional[int] = None
    bhav_changed: Optional[int] = None
    bhav_unchanged: Optional[int] = None
    bhav_skipped_unknown_isin: Optional[int] = None
    bhav_skipped_no_price: Optional[int] = None
    bhav_availability_written: Optional[int] = None
    bhav_coupon_hints_written: Optional[int] = None
    corp_rows: Optional[int] = None
    corp_new: Optional[int] = None
    corp_changed: Optional[int] = None
    corp_unchanged: Optional[int] = None
    corp_skipped_unknown_isin: Optional[int] = None
    corp_seeded_new_instruments: Optional[int] = None
    corp_trade_date: Optional[str] = None
    hints_new: Optional[int] = None
    hints_changed: Optional[int] = None
    hints_unchanged: Optional[int] = None
    hints_unparseable: Optional[int] = None
    hints_issuer_only: Optional[int] = None
    csv: list[CsvSummary] = field(default_factory=list)
    screen: dict = field(default_factory=dict)
    screen_reused: bool = False
    report_dir: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def ist_today() -> date:
    return datetime.now(IST).date()


def default_connectors(settlement: date) -> dict[str, Any]:
    return {
        "universe": NseDebtUniverse(),
        "bhavcopy": NseCmBhavcopy(settlement),
        "corp_trades": NseCorpBondTrades(),
        "bse": BseDebt(),
    }


# ---------------------------------------------------------------- change detection helpers

def _latest_record(conn: sqlite3.Connection, source: str, url: Optional[str]):
    return conn.execute(
        "SELECT fetched_at, sha256, status, note FROM source_record "
        "WHERE source = ? AND url IS ? ORDER BY id DESC LIMIT 1",
        (source, url),
    ).fetchone()


def _unchanged_since(conn: sqlite3.Connection, rec: SourceRecord) -> Optional[str]:
    """fetched_at of the latest ok record with identical bytes for the same source+url, else None."""
    if rec.status != STATUS_OK or not rec.sha256:
        return None
    last = _latest_record(conn, rec.source, rec.url)
    if last is not None and last["status"] == STATUS_OK and last["sha256"] == rec.sha256:
        return last["fetched_at"]
    return None


def _discard_duplicate_raw(rec: SourceRecord, connector) -> None:
    """Delete the raw file a connector just saved when its bytes were already ingested.

    Only files under the connector's own raw_root are removed; anything else is left alone.
    """
    root = getattr(connector, "raw_root", None)
    if root is None or not rec.raw_path:
        return
    path = Path(rec.raw_path).resolve()
    try:
        path.relative_to(Path(root).resolve())
    except ValueError:
        return
    path.unlink(missing_ok=True)


def _record_if_new(conn: sqlite3.Connection, rec: SourceRecord) -> bool:
    """Persist a source_record. Non-ok records repeating the previous outcome are not re-recorded."""
    if rec.status != STATUS_OK:
        last = conn.execute(
            "SELECT status, note FROM source_record WHERE source = ? ORDER BY id DESC LIMIT 1",
            (rec.source,),
        ).fetchone()
        if last is not None and last["status"] == rec.status and last["note"] == rec.note:
            return False
    pipeline.persist_source_record(conn, rec)
    conn.commit()
    return True


def _differs(a, b) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(float(a) - float(b)) > 1e-9


# ---------------------------------------------------------------- source steps

_PARSE_FAIL_NOTE = "parse failed: {}: {}"


def _parse_failure(conn, summary: RefreshSummary, rec: SourceRecord, exc: BaseException) -> str:
    failed = SourceRecord(
        source=rec.source, url=rec.url, fetched_at=rec.fetched_at, content_type=rec.content_type,
        size_bytes=rec.size_bytes, sha256=rec.sha256, raw_path=rec.raw_path, status="error",
        note=_PARSE_FAIL_NOTE.format(type(exc).__name__, exc),
    )
    if _record_if_new(conn, failed):
        summary.db_writes += 1
    return failed.note


def _load_universe(conn, summary: RefreshSummary, connector) -> None:
    rec = connector.fetch()
    st = SourceStatus(rec.source, rec.status, rec.note, rec.size_bytes)
    summary.sources.append(st)
    if rec.status != STATUS_OK:
        if _record_if_new(conn, rec):
            summary.db_writes += 1
        summary.warnings.append(
            f"{rec.source} {rec.status}: {rec.note}; continuing with stored instrument data"
        )
        return

    since = _unchanged_since(conn, rec)
    if since is not None:
        _discard_duplicate_raw(rec, connector)
        st.note = f"unchanged since {since}; not re-ingested"
        return

    try:
        rows = connector.parse_file(rec.raw_path)
    except _PARSE_ERRORS as exc:
        note = _parse_failure(conn, summary, rec, exc)
        summary.sources[-1] = SourceStatus(rec.source, "error", note, rec.size_bytes)
        summary.warnings.append(f"{rec.source} parse failed: {note}; continuing with stored data")
        return
    result = pipeline.ingest_nse_universe(conn, rows, rec)
    summary.db_writes += 1
    summary.universe_rows = result["instruments"]


def _write_bhav_changes(conn, summary: RefreshSummary, rec: SourceRecord, rows) -> None:
    """Write only new or changed price rows and changed availability flags. One transaction."""
    counts = {"new": 0, "changed": 0, "unchanged": 0, "unknown": 0, "no_price": 0,
              "avail": 0, "hints": 0}
    try:
        source_id = pipeline.persist_source_record(conn, rec)
        summary.db_writes += 1
        for row in rows:
            isin = row["isin"]
            if not db.instrument_exists(conn, isin):
                counts["unknown"] += 1
                continue

            hint = row.get("coupon_hint")
            if hint is not None:
                cur = conn.execute("SELECT coupon_hint FROM instrument WHERE isin = ?", (isin,)).fetchone()
                if _differs(cur["coupon_hint"], hint):
                    db.upsert_instrument(conn, isin, coupon_hint=hint)
                    counts["hints"] += 1

            trade_date = row.get("trade_date")
            close = row.get("close")
            if not trade_date or close is None:
                counts["no_price"] += 1
                continue
            volume = row.get("volume")

            last = conn.execute(
                "SELECT clean_price, volume FROM price_snapshot WHERE isin = ? "
                "AND kind = 'exchange_close' AND trade_date IS ? ORDER BY id DESC LIMIT 1",
                (isin, trade_date),
            ).fetchone()
            if last is None:
                status = "new"
            elif _differs(last["clean_price"], close) or _differs(last["volume"], volume):
                status = "changed"
            else:
                status = "unchanged"
            counts[status] += 1
            if status == "unchanged":
                continue

            db.insert_price_snapshot(
                conn,
                isin=isin,
                trade_date=trade_date,
                clean_price=close,
                volume=volume,
                value=row.get("value"),
                trades=row.get("trades"),
                kind="exchange_close",
                platform=None,
                vendor_ytm=None,
                freshness=pipeline.EXCHANGE_CLOSE_FRESHNESS,
                source_id=source_id,
                observed_at=rec.fetched_at,
                source_url=rec.url,
            )
            summary.db_writes += 1

            # Listed on the exchange; a trade is not a verified executable purchase.
            flags = (1, None if volume is None else int(volume > 0), None, 0, 0)
            prev = conn.execute(
                "SELECT listed, recently_traded, offered_for_sale, price_verified, executable_verified "
                "FROM availability WHERE isin = ? ORDER BY id DESC LIMIT 1",
                (isin,),
            ).fetchone()
            if prev is None or tuple(prev) != flags:
                db.insert_availability(
                    conn,
                    isin=isin,
                    listed=flags[0],
                    recently_traded=flags[1],
                    offered_for_sale=flags[2],
                    price_verified=flags[3],
                    executable_verified=flags[4],
                    min_investment=None,
                    platform=None,
                    observed_at=rec.fetched_at,
                    source_id=source_id,
                    source_url=rec.url,
                )
                counts["avail"] += 1
                summary.db_writes += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    summary.bhav_new = counts["new"]
    summary.bhav_changed = counts["changed"]
    summary.bhav_unchanged = counts["unchanged"]
    summary.bhav_skipped_unknown_isin = counts["unknown"]
    summary.bhav_skipped_no_price = counts["no_price"]
    summary.bhav_availability_written = counts["avail"]
    summary.bhav_coupon_hints_written = counts["hints"]


def _load_bhavcopy(conn, summary: RefreshSummary, connector, settlement: date) -> None:
    rec = connector.fetch()
    st = SourceStatus(rec.source, rec.status, rec.note, rec.size_bytes)
    summary.sources.append(st)

    resolved = getattr(connector, "resolved_date", None) if rec.status == STATUS_OK else None
    if resolved is not None:
        summary.bhavcopy_trade_date = resolved.isoformat()
        summary.bhavcopy_walkback_days = max((settlement - resolved).days, 0)
        if summary.bhavcopy_walkback_days:
            summary.warnings.append(
                f"bhavcopy walked back {summary.bhavcopy_walkback_days} day(s): no file for "
                f"{settlement.isoformat()}, used {resolved.isoformat()}"
            )

    if rec.status != STATUS_OK:
        if _record_if_new(conn, rec):
            summary.db_writes += 1
        summary.warnings.append(
            f"{rec.source} {rec.status}: {rec.note}; continuing with last stored prices"
        )
        return

    since = _unchanged_since(conn, rec)
    if since is not None:
        _discard_duplicate_raw(rec, connector)
        st.note = f"unchanged since {since}; no price rows processed"
        summary.bhavcopy_file_unchanged = True
        return

    try:
        rows = connector.parse_file(rec.raw_path)
    except _PARSE_ERRORS as exc:
        note = _parse_failure(conn, summary, rec, exc)
        summary.sources[-1] = SourceStatus(rec.source, "error", note, rec.size_bytes)
        summary.warnings.append(f"{rec.source} parse failed: {note}; continuing with last stored prices")
        return

    if resolved is None and rows and rows[0].get("trade_date"):
        d = date.fromisoformat(rows[0]["trade_date"])
        summary.bhavcopy_trade_date = d.isoformat()
        summary.bhavcopy_walkback_days = max((settlement - d).days, 0)
    _write_bhav_changes(conn, summary, rec, rows)


def _latest_source_id(conn, source: str, url: Optional[str]) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM source_record WHERE source = ? AND url IS ? ORDER BY id DESC LIMIT 1",
        (source, url),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _load_corp_trades(conn, summary: RefreshSummary, connector, settlement: date) -> None:
    """NSE OTC/RFQ corp trades: one record per index; changed indices recorded and ingested together."""
    recs = list(connector.fetch())
    ok: list[tuple[SourceRecord, SourceStatus]] = []
    for rec in recs:
        label = f"{rec.source} ({index_from_url(rec.url) or rec.url})"
        st = SourceStatus(label, rec.status, rec.note, rec.size_bytes)
        summary.sources.append(st)
        if rec.status != STATUS_OK:
            if _record_if_new(conn, rec):
                summary.db_writes += 1
            summary.warnings.append(f"{label} {rec.status}: {rec.note}; continuing with stored data")
            continue
        ok.append((rec, st))

    changed: list[SourceRecord] = []
    existing_ids: dict[str, int] = {}
    rows: list[dict] = []
    for rec, st in ok:
        since = _unchanged_since(conn, rec)
        try:
            parsed = connector.parse_record(rec)
        except _PARSE_ERRORS as exc:
            note = _parse_failure(conn, summary, rec, exc)
            st.status, st.note = "error", note
            summary.warnings.append(f"{st.name} parse failed: {note}; continuing with stored data")
            continue
        rows.extend(parsed)
        if since is None:
            changed.append(rec)
        else:
            _discard_duplicate_raw(rec, connector)
            st.note = f"unchanged since {since}; not re-ingested"
            existing_ids[rec.url] = _latest_source_id(conn, rec.source, rec.url)

    counts = pipeline.ingest_nse_corp_trades(conn, rows, changed, existing_ids)
    summary.db_writes += counts["records_persisted"] + counts["seeded_new_instruments"] \
        + counts["new"] + counts["changed"] + counts["availability_written"]
    summary.corp_rows = counts["rows"]
    summary.corp_new = counts["new"]
    summary.corp_changed = counts["changed"]
    summary.corp_unchanged = counts["unchanged"]
    summary.corp_skipped_unknown_isin = counts["skipped_unknown_isin"]
    summary.corp_seeded_new_instruments = counts["seeded_new_instruments"]

    dates = [r["trade_date"] for r in rows if r.get("trade_date")]
    if dates:
        newest = max(dates)
        summary.corp_trade_date = newest
        age = (settlement - date.fromisoformat(newest)).days
        if age > CORP_TRADES_MAX_AGE_DAYS:
            summary.warnings.append(
                f"corp-trades: newest NSE response timestamp date {newest} is {age} day(s) before "
                f"settlement {settlement.isoformat()} (older than {CORP_TRADES_MAX_AGE_DAYS} days); "
                "corporate trade prices may be stale"
            )


def _latest_ok_source_id(conn, source: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM source_record WHERE source = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _load_hints(conn, summary: RefreshSummary) -> None:
    """Parse NSE descriptor names of INE instruments into UNVERIFIED descriptor_hint rows.

    DB-only (no network). A name that yields none of coupon/maturity/face is 'unparseable';
    of those, a name with no instrument-kind token and no FVRS token is 'issuer-only'.
    Hints are written only when new or changed; source_id = latest ok nse_corp_trades record.
    """
    source_id = _latest_ok_source_id(conn, NseCorpBondTrades.source)
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"new": 0, "changed": 0, "unchanged": 0, "unparseable": 0, "issuer_only": 0}
    names = conn.execute(
        "SELECT isin, name FROM instrument WHERE isin LIKE 'INE%' AND name IS NOT NULL ORDER BY isin"
    ).fetchall()
    for isin, name in names:
        d = parse_descriptor(name)
        if d.coupon_pct is None and d.maturity_date is None and d.face_value is None:
            counts["unparseable"] += 1
            if d.instrument_kind is None and "FVRS" not in name.upper():
                counts["issuer_only"] += 1
            continue
        status = db.upsert_descriptor_hint(conn, isin, d, source_id, observed_at)
        counts[status] += 1
    conn.commit()
    summary.hints_new = counts["new"]
    summary.hints_changed = counts["changed"]
    summary.hints_unchanged = counts["unchanged"]
    summary.hints_unparseable = counts["unparseable"]
    summary.hints_issuer_only = counts["issuer_only"]
    summary.db_writes += counts["new"] + counts["changed"]


def _record_bse(conn, summary: RefreshSummary, connector) -> None:
    rec = connector.fetch()
    summary.sources.append(SourceStatus(rec.source, rec.status, rec.note, rec.size_bytes))
    if _record_if_new(conn, rec):
        summary.db_writes += 1
    if rec.status != STATUS_OK:
        summary.warnings.append(f"{rec.source} {rec.status}: {rec.note}")


# ---------------------------------------------------------------- CSV inputs

def _import_csvs(conn, summary: RefreshSummary, inputs_dir: Path) -> None:
    for fname, importer, _header, source in CSV_INPUTS:
        path = inputs_dir / fname
        cs = CsvSummary(name=fname, provided=path.is_file())
        summary.csv.append(cs)
        if not cs.provided:
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            cs.error_count = 1
            cs.errors = [f"cannot read file: {exc}"]
            summary.warnings.append(f"csv {fname}: cannot read file")
            continue
        sha = hashlib.sha256(raw).hexdigest()
        last = _latest_record(conn, source, ci.source_label(path))
        if last is not None and last["sha256"] == sha:
            cs.unchanged_since = f"{last['fetched_at']} (status {last['status']})"
            continue
        try:
            rep = importer(conn, path)
        except OSError as exc:
            cs.error_count = 1
            cs.errors = [f"cannot read file: {exc}"]
            summary.warnings.append(f"csv {fname}: cannot read file")
            continue
        summary.db_writes += 1
        cs.rows_read = rep.rows_read
        cs.rows_ok = rep.rows_ok
        cs.duplicates = rep.duplicates
        cs.error_count = len(rep.errors)
        cs.errors = [f"line {line}: {msg}" for line, msg in rep.errors[:MAX_ERRORS_KEPT]]
        if cs.error_count:
            summary.warnings.append(f"csv {fname}: {cs.error_count} row(s) rejected, see summary")
    if not any(c.name == "instrument_master.csv" and c.provided for c in summary.csv):
        summary.warnings.append(NO_MASTER_WARNING)


# ---------------------------------------------------------------- screen reuse

def _has_screen(conn, run_id: str) -> bool:
    return conn.execute("SELECT 1 FROM screen_result WHERE run_id = ? LIMIT 1", (run_id,)).fetchone() is not None


def _stored_screen_summary(conn, run_id: str, settlement: date) -> dict:
    """Same shape as screener.run_screen output, computed from rows already stored."""
    rows = conn.execute("SELECT bucket, ytm FROM screen_result WHERE run_id = ?", (run_id,)).fetchall()
    buckets = {"primary": 0, "speculative": 0, "rejected": 0}
    for r in rows:
        buckets[r["bucket"]] += 1
    rules = {r["rule"]: r["n"] for r in conn.execute(
        "SELECT rule, COUNT(*) AS n FROM rejection WHERE run_id = ? GROUP BY rule", (run_id,))}
    return {
        "run_id": run_id,
        "settlement": settlement.isoformat(),
        "instruments": len(rows),
        "buckets": buckets,
        "rejections": dict(sorted(rules.items())),
        "ytm_computed": sum(1 for r in rows if r["ytm"] is not None),
    }


# ---------------------------------------------------------------- orchestration

def _run(conn, summary: RefreshSummary, settlement: date, offline: bool, inputs_dir: Path,
         out_dir: Path, make_report: bool, connectors: Optional[Mapping[str, Any]],
         corp_trades: bool = True, hints: bool = True) -> None:
    if offline:
        for name in (NseDebtUniverse.source, NseCmBhavcopy.source, NseCorpBondTrades.source, BseDebt.source):
            summary.sources.append(SourceStatus(name, "skipped", "--offline"))
    else:
        conns = {**default_connectors(settlement), **(connectors or {})}
        _load_universe(conn, summary, conns["universe"])
        _load_bhavcopy(conn, summary, conns["bhavcopy"], settlement)
        if corp_trades:
            _load_corp_trades(conn, summary, conns["corp_trades"], settlement)
        else:
            summary.sources.append(SourceStatus(NseCorpBondTrades.source, "skipped", "--no-corp-trades"))
        _record_bse(conn, summary, conns["bse"])

    if hints:
        _load_hints(conn, summary)

    _import_csvs(conn, summary, inputs_dir)

    run_id = f"screen-{settlement.isoformat()}"
    if summary.db_writes == 0 and _has_screen(conn, run_id):
        summary.screen = _stored_screen_summary(conn, run_id, settlement)
        summary.screen_reused = True
    else:
        summary.screen = run_screen(conn, settlement, run_id=run_id)

    stale = summary.screen.get("rejections", {}).get("stale_price", 0)
    if stale:
        summary.warnings.append(
            f"{stale} prices older than {STALE_PRICE_DAYS} days -> stale_price rejections"
        )

    if make_report:
        try:
            result = write_report(conn, out_dir, run_id=run_id)
            summary.report_dir = str(result.day_dir)
        except NoRunError as exc:
            summary.warnings.append(f"report skipped: {exc}")


def run_refresh(
    db_path,
    settlement: date,
    offline: bool,
    inputs_dir,
    out_dir,
    make_report: bool,
    connectors: Optional[Mapping[str, Any]] = None,
    corp_trades: bool = True,
    hints: bool = True,
) -> RefreshSummary:
    """Run one refresh. Raises DatabaseOpenError if the DB cannot be created or opened.

    connectors may override any of the keys 'universe', 'bhavcopy', 'corp_trades', 'bse'
    (tests inject fakes). corp_trades=False skips the NSE OTC/RFQ source entirely.
    hints=False skips the descriptor hints step (DB-only; runs in offline mode too).
    """
    db_path = Path(db_path)
    summary = RefreshSummary(db_path=str(db_path), settlement=settlement.isoformat(), offline=offline)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
        except BaseException:
            conn.close()
            raise
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseOpenError(f"cannot open database {db_path}: {exc}") from exc

    try:
        _run(conn, summary, settlement, offline, Path(inputs_dir), Path(out_dir), make_report, connectors,
             corp_trades=corp_trades, hints=hints)
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------- inputs templates

def init_inputs(inputs_dir) -> dict[str, str]:
    """Create header-only CSV templates that do not exist yet. Never overwrites."""
    inputs_dir = Path(inputs_dir)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for fname, _importer, header, _source in CSV_INPUTS:
        path = inputs_dir / fname
        try:
            with path.open("x", newline="", encoding="utf-8") as fh:
                csv.writer(fh, lineterminator="\n").writerow(header)
            result[fname] = "created"
        except FileExistsError:
            result[fname] = "exists, not overwritten"
    return result


# ---------------------------------------------------------------- printing

def format_summary(s: RefreshSummary) -> str:
    lines = [
        f"refresh settlement={s.settlement} db={s.db_path} offline={'yes' if s.offline else 'no'}",
        "",
        "sources:",
    ]
    for src in s.sources:
        size = "" if src.size_bytes is None else f" size={src.size_bytes} B"
        note = "" if not src.note else f" note={src.note}"
        lines.append(f"  {src.name}: {src.status}{size}{note}")

    lines.append("")
    if s.offline:
        lines.append("universe rows loaded: skipped (--offline)")
        lines.append("prices: skipped (--offline)")
    else:
        if s.universe_rows is not None:
            lines.append(f"universe rows loaded: {s.universe_rows}")
        else:
            lines.append("universe rows loaded: none (stored data used)")
        if s.bhavcopy_trade_date is None:
            lines.append("bhavcopy trade date used: none")
        else:
            lines.append(
                f"bhavcopy trade date used: {s.bhavcopy_trade_date} "
                f"(walked back {s.bhavcopy_walkback_days} day(s))"
            )
        if s.bhavcopy_file_unchanged:
            lines.append("prices: bhavcopy file unchanged, no price rows processed")
        elif s.bhav_new is not None:
            lines.append(
                f"prices: new={s.bhav_new} changed={s.bhav_changed} unchanged={s.bhav_unchanged} "
                f"skipped_unknown_isin={s.bhav_skipped_unknown_isin} "
                f"skipped_no_price={s.bhav_skipped_no_price} "
                f"availability_written={s.bhav_availability_written} "
                f"coupon_hints_written={s.bhav_coupon_hints_written}"
            )
        if s.corp_rows is not None:
            date_note = f" response_date={s.corp_trade_date}" if s.corp_trade_date else ""
            lines.append(
                f"corp-trades (OTC/RFQ): rows={s.corp_rows} new={s.corp_new} changed={s.corp_changed} "
                f"unchanged={s.corp_unchanged} skipped_unknown_isin={s.corp_skipped_unknown_isin} "
                f"seeded_new_instruments={s.corp_seeded_new_instruments}"
                f"{date_note} (prices as reported by NSE; clean-price basis and trade-value unit unverified)"
            )

    if s.hints_new is not None:
        lines.append(
            f"descriptor hints: new={s.hints_new} changed={s.hints_changed} "
            f"unchanged={s.hints_unchanged} unparseable={s.hints_unparseable} "
            f"(issuer-only names: {s.hints_issuer_only})"
        )

    lines.append("")
    lines.append("csv imports:")
    for cs in s.csv:
        if not cs.provided:
            lines.append(f"  {cs.name}: not provided")
            continue
        if cs.unchanged_since:
            lines.append(f"  {cs.name}: unchanged since {cs.unchanged_since}; not re-imported")
            continue
        lines.append(
            f"  {cs.name}: rows_read={cs.rows_read} rows_ok={cs.rows_ok} "
            f"duplicates={cs.duplicates} errors={cs.error_count}"
        )
        for err in cs.errors:
            lines.append(f"    {err}")

    lines.append("")
    if s.screen:
        b = s.screen.get("buckets", {})
        reused = " (inputs unchanged; stored result reused)" if s.screen_reused else ""
        lines.append(
            f"screen {s.screen.get('run_id')}{reused}: instruments={s.screen.get('instruments')} "
            f"primary={b.get('primary', 0)} speculative={b.get('speculative', 0)} "
            f"rejected={b.get('rejected', 0)} ytm_computed={s.screen.get('ytm_computed')}"
        )
        rules = sorted(s.screen.get("rejections", {}).items(), key=lambda kv: (-kv[1], kv[0]))
        if rules:
            lines.append("top rejection rules:")
            for rule, n in rules[:TOP_RULES]:
                lines.append(f"  {rule}: {n}")
    lines.append("")
    if s.db_writes == 0:
        lines.append("DB unchanged: nothing written (all sources identical to stored records)")
    else:
        lines.append(f"DB writes: {s.db_writes} row(s)")
    lines.append(f"report: {s.report_dir if s.report_dir else 'not written'}")

    lines.append("")
    if s.warnings:
        lines.append(f"WARNINGS ({len(s.warnings)}):")
        for w in s.warnings:
            lines.append(f"  - {w}")
    else:
        lines.append("WARNINGS: none")
    return "\n".join(lines)
