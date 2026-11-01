"""Pipeline glue: persist connector output (NSE universe, NSE bhavcopy) into the database.

- Connectors (data_sources) never touch the DB; this module does.
- Only instrument fields explicitly listed here are written. Coupon, maturity,
  face value, ratings and every other master field are left alone or NULL.
- ingest_* functions validate the SourceRecord first, then write inside one
  transaction and commit on success (rollback on any exception).
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping

from india_bonds.data_sources.base import STATUS_OK, SourceRecord
from india_bonds.data_sources.nse import select_one_per_isin
from india_bonds.database import db

EXCHANGE_CLOSE_FRESHNESS = "delayed"
CORP_TRADES_KIND = "exchange_reported"
CORP_TRADES_FRESHNESS = "delayed"
# Corp-feed ISINs missing from instrument are seeded only when they start with 'INE' (Indian
# corporate issuers). Other prefixes are skipped and counted as skipped_unknown_isin.
CORP_SEED_PREFIX = "INE"


def persist_source_record(conn: sqlite3.Connection, rec: SourceRecord) -> int:
    """Insert a connector SourceRecord into source_record and return its id. Does not commit."""
    return db.insert_source_record(
        conn,
        source=rec.source,
        url=rec.url,
        fetched_at=rec.fetched_at,
        content_type=rec.content_type,
        size_bytes=rec.size_bytes,
        sha256=rec.sha256,
        raw_path=rec.raw_path,
        status=rec.status,
        note=rec.note,
    )


def _require_ok(rec: SourceRecord) -> None:
    if rec.status != STATUS_OK:
        raise ValueError(
            f"refusing to ingest source record with status {rec.status!r} from {rec.url!r}: {rec.note}"
        )


def ingest_nse_universe(
    conn: sqlite3.Connection,
    rows: Iterable[Mapping],
    source_record: SourceRecord,
) -> dict:
    """Upsert listed instruments from parsed NSE universe rows.

    rows: dicts with keys isin, name, instrument_type (NseDebtUniverse.parse output).
    Returns {'instruments': n, 'source_id': id}.
    """
    _require_ok(source_record)
    retrieved_on = source_record.fetched_at[:10]
    count = 0
    try:
        source_id = persist_source_record(conn, source_record)
        for row in rows:
            fields = {
                "instrument_type": row["instrument_type"] or None,
                "exchange_listed": 1,
                "source_url": source_record.url,
                "retrieved_on": retrieved_on,
                "master_source_id": source_id,
            }
            if row.get("name"):
                fields["name"] = row["name"]
            db.upsert_instrument(conn, row["isin"], **fields)
            count += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"instruments": count, "source_id": source_id}


def ingest_nse_bhavcopy(
    conn: sqlite3.Connection,
    rows: Iterable[Mapping],
    source_record: SourceRecord,
) -> dict:
    """Store exchange closes, coupon hints and availability for ISINs already in instrument.

    rows: dicts with keys isin, trade_date, close, volume, value, trades, coupon_hint
    (NseCmBhavcopy.parse_rows output).

    - ISINs not present in instrument are skipped (never created here).
    - coupon_hint is written to instrument.coupon_hint only; coupon_pct is untouched.
    - A price_snapshot is skipped when one already exists for the same isin, trade_date
      and kind='exchange_close'. An availability row is written only with a new snapshot.

    Returns {'inserted', 'skipped_unknown_isin', 'skipped_duplicate', 'skipped_no_price', 'source_id'}.
    """
    _require_ok(source_record)
    counts = {"inserted": 0, "skipped_unknown_isin": 0, "skipped_duplicate": 0, "skipped_no_price": 0}
    observed_at = source_record.fetched_at
    try:
        source_id = persist_source_record(conn, source_record)
        for row in rows:
            isin = row["isin"]
            if not db.instrument_exists(conn, isin):
                counts["skipped_unknown_isin"] += 1
                continue

            hint = row.get("coupon_hint")
            if hint is not None:
                db.upsert_instrument(conn, isin, coupon_hint=hint)

            trade_date = row.get("trade_date")
            close = row.get("close")
            if not trade_date or close is None:
                counts["skipped_no_price"] += 1
                continue

            dup = conn.execute(
                "SELECT 1 FROM price_snapshot WHERE isin = ? AND kind = 'exchange_close' "
                "AND trade_date IS ? LIMIT 1",
                (isin, trade_date),
            ).fetchone()
            if dup is not None:
                counts["skipped_duplicate"] += 1
                continue

            volume = row.get("volume")
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
                freshness=EXCHANGE_CLOSE_FRESHNESS,
                source_id=source_id,
                observed_at=observed_at,
                source_url=source_record.url,
            )
            # Listed on the exchange; a trade is not a verified executable purchase.
            db.insert_availability(
                conn,
                isin=isin,
                listed=1,
                recently_traded=None if volume is None else int(volume > 0),
                offered_for_sale=None,
                price_verified=0,
                executable_verified=0,
                min_investment=None,
                platform=None,
                observed_at=observed_at,
                source_id=source_id,
                source_url=source_record.url,
            )
            counts["inserted"] += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    counts["source_id"] = source_id
    return counts


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= 1e-9


def _fmt(value) -> str:
    return "NA" if value is None else repr(float(value)) if isinstance(value, float) else str(value)


def ingest_nse_corp_trades(
    conn: sqlite3.Connection,
    rows: Iterable[Mapping],
    records: Iterable[SourceRecord] = (),
    existing_source_ids: Mapping[str, int] | None = None,
) -> dict:
    """Store NSE OTC/RFQ reported prices (kind='exchange_reported') for ISINs already in instrument.

    rows: NseCorpBondTrades.parse output for every index in this run (both indices, so the
    per-ISIN choice is made across them). records: ok SourceRecords to persist in this same
    transaction (indices whose bytes changed). existing_source_ids: url -> source_record id for
    indices unchanged since their last ok record. Every row's source_url must map to one of these.

    - One row per ISIN is ingested: the one with more trades (tie: larger trade_value), see
      select_one_per_isin. The other index's row is counted as dropped_duplicate.
    - Unknown ISINs with prefix 'INE' are seeded into instrument (name from descriptor,
      instrument_type='DEBT', exchange_listed=1, source_url, retrieved_on = response date,
      master_source_id = record id). Every other field stays NULL: no master data is implied, so
      such instruments are rejected by eligibility (missing coupon/schedule, unverified flags).
      Other unknown prefixes are skipped (skipped_unknown_isin).
    - Price: price_per_100 = ltp (per 100 face; clean-price basis unverified); clean_price stays NULL.
      reported_yield_pct = lty (percent). value = tradeValue raw (unit unverified). volume NULL.
    - Change-aware: a snapshot is inserted only when (isin, trade_date, kind) has no row yet or its
      price_per_100 / reported_yield_pct / trades differ from the latest such row.
    - An availability row (listed=1, recently_traded, price_verified=0, executable_verified=0) is
      written with a new snapshot, only when its flags differ from the latest availability row.
    - coupon_pct is never touched. ISINs not in instrument are skipped, never created.

    Returns counts: rows, selected, dropped_duplicate, skipped_no_price, skipped_unknown_isin,
    seeded_new_instruments, new, changed, unchanged, availability_written, records_persisted.
    """
    rows = list(rows)
    records = list(records)
    for rec in records:
        _require_ok(rec)
    counts = {
        "rows": len(rows), "selected": 0, "dropped_duplicate": 0, "skipped_no_price": 0,
        "skipped_unknown_isin": 0, "seeded_new_instruments": 0, "new": 0, "changed": 0,
        "unchanged": 0, "availability_written": 0, "records_persisted": 0,
    }
    try:
        source_ids = dict(existing_source_ids or {})
        for rec in records:
            source_ids[rec.url] = persist_source_record(conn, rec)
            counts["records_persisted"] += 1

        selected, dropped, no_price = select_one_per_isin(rows)
        counts["dropped_duplicate"] = dropped
        counts["skipped_no_price"] = no_price
        for row in selected:
            isin = row["isin"]
            source_id = source_ids.get(row["source_url"])
            if source_id is None:
                raise ValueError(f"no source_record for {row['source_url']!r}; cannot ingest {isin}")
            if not db.instrument_exists(conn, isin):
                if not isin.startswith(CORP_SEED_PREFIX):
                    counts["skipped_unknown_isin"] += 1
                    continue
                db.upsert_instrument(
                    conn, isin,
                    name=row["name"] or None,
                    instrument_type="DEBT",
                    exchange_listed=1,
                    source_url=row["source_url"],
                    retrieved_on=row["trade_date"],
                    master_source_id=source_id,
                )
                counts["seeded_new_instruments"] += 1
            counts["selected"] += 1

            ltp, lty, trades = row["ltp"], row["lty"], row["trades"]
            trade_date = row["trade_date"]
            last = conn.execute(
                "SELECT price_per_100, reported_yield_pct, trades FROM price_snapshot "
                "WHERE isin = ? AND kind = ? AND trade_date IS ? ORDER BY id DESC LIMIT 1",
                (isin, CORP_TRADES_KIND, trade_date),
            ).fetchone()
            if last is None:
                status = "new"
            elif (not _same(last["price_per_100"], ltp) or not _same(last["reported_yield_pct"], lty)
                  or last["trades"] != trades):
                status = "changed"
            else:
                status = "unchanged"
            counts[status] += 1
            if status == "unchanged":
                continue

            notes = (f"wap={_fmt(row['wap'])};way={_fmt(row['way'])};"
                     f"tradeValue={_fmt(row['trade_value'])}(unit unverified);trades={_fmt(trades)}")
            db.insert_price_snapshot(
                conn,
                isin=isin,
                trade_date=trade_date,
                clean_price=None,
                volume=None,
                value=row["trade_value"],
                trades=trades,
                kind=CORP_TRADES_KIND,
                platform=None,
                vendor_ytm=None,
                freshness=CORP_TRADES_FRESHNESS,
                source_id=source_id,
                observed_at=row["response_timestamp"],
                source_url=row["source_url"],
                notes=notes,
                price_per_100=ltp,
                reported_yield_pct=lty,
            )

            # Listed and reported as traded by NSE; not a verified executable price.
            flags = (1, None if trades is None else int(trades > 0), None, 0, 0)
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
                    observed_at=row["response_timestamp"],
                    source_id=source_id,
                    source_url=row["source_url"],
                )
                counts["availability_written"] += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return counts
