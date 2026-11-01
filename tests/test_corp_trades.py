"""Tests for the NSE OTC/RFQ corporate bond trades source (NseCorpBondTrades) and its wiring.

HTTP is mocked (no network). All ISINs, names and prices are SYNTHETIC test data.
Labels such as 'basis unverified' are asserted because the code must never present the NSE
price basis or trade-value unit as verified.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from india_bonds import pipeline
from india_bonds.analytics.ytm import compute_ytm
from india_bonds.dashboard import queries as q
from india_bonds.data_sources.base import STATUS_ERROR, STATUS_OK, STATUS_UNAVAILABLE, SourceRecord
from india_bonds.data_sources.nse import (
    CORP_TRADES_INDICES,
    NseCorpBondTrades,
    corp_trades_url,
    index_from_url,
    parse_corp_timestamp,
    select_one_per_isin,
)
from india_bonds.database import db
from india_bonds.reports import markdown as md_report
from india_bonds.refresh import format_summary, run_refresh
from india_bonds.screening.eligibility import evaluate, latest_price
from india_bonds.screening.screener import run_screen

SETTLE = date(2026, 10, 30)
SOURCE = "SYNTHETIC TEST DATA"
FETCHED = "2026-10-30T12:00:00+00:00"

OTC_URL = corp_trades_url("otctrades_listed")
RFQ_URL = corp_trades_url("rfqtrades_listed")

A = "INE000T00020"   # in instrument; traded on both indices (RFQ has more trades)
B = "INE000T00021"   # in instrument; OTC only
UNKNOWN = "INE000T00029"  # not in instrument
GOV = "IN0020170059"  # sovereign, must be dropped

BOND = dict(
    name="SYNTHETIC BOND 9.00% 2030", exchange_listed=1, instrument_type="NCD",
    face_value=1000.0, coupon_pct=9.0, coupon_frequency="annual", first_interest_date=None,
    maturity_date="2030-10-30", issue_date="2025-10-30", security_type="secured",
    seniority="senior", collateral_text=None, is_perpetual=0, is_at1=0, is_structured=0,
    default_flag=0, trading_suspended=0, currency="INR", source_url=SOURCE, retrieved_on="2026-10-31",
)


def _otc_payload(timestamp="30-Oct-2026 17:13"):
    return {
        "data": [
            {"descriptor": "SYNTHETIC A SR 1 9 NCD 30OC30\r", "isin": A, "ltp": 98.5,
             "lty": 9.6, "noOfTrades": 5, "tradeValue": 5000, "wap": 98.4, "way": 9.7},
            {"descriptor": "SYNTHETIC B SR 2 9 NCD 30OC30\r", "isin": B, "ltp": 101.0,
             "lty": 8.1, "noOfTrades": 2, "tradeValue": 2000, "wap": 101.1, "way": 8.0},
            {"descriptor": "GOVT\r", "isin": GOV, "ltp": 100.0, "lty": 7.0, "noOfTrades": 9,
             "tradeValue": 9000, "wap": 100.0, "way": 7.0},
            {"descriptor": "BAD\r", "isin": "NOTANISIN", "ltp": 100.0, "lty": 7.0,
             "noOfTrades": 1, "tradeValue": 1, "wap": 100.0, "way": 7.0},
        ],
        "timestamp": timestamp,
        "marketStatus": {"market": "CBM"},
    }


def _rfq_payload(timestamp="30-Oct-2026 17:13:00"):
    return {
        "data": [
            {"descriptor": "SYNTHETIC A SR 1 9 NCD 30OC30\r", "isin": A, "ltp": 98.7,
             "lty": 9.5, "noOfTrades": 9, "tradeValue": 8000, "wap": 98.6, "way": 9.5},
        ],
        "timestamp": timestamp,
    }


class FakeResp:
    def __init__(self, status=200, content=b"", ctype="application/json; charset=utf-8"):
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": ctype} if ctype else {}


def _json_resp(payload):
    return FakeResp(200, json.dumps(payload).encode("utf-8"))


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.urls: list[str] = []
        self.headers: dict = {}

    def get(self, url, timeout=None):
        self.urls.append(url)
        result = self.handler(url)
        if isinstance(result, BaseException):
            raise result
        return result


def _ok_handler(url):
    return _json_resp(_otc_payload() if "otctrades" in url else _rfq_payload())


def _connector(tmp_path, handler=_ok_handler, sleep=lambda s: None, clock=None):
    session = FakeSession(handler)
    kwargs = {"clock": clock} if clock is not None else {}
    conn = NseCorpBondTrades(session=session, raw_root=tmp_path / "raw", sleep=sleep, **kwargs)
    return conn, session


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "corp.sqlite")
    db.init_schema(c)
    for isin in (A, B):
        db.upsert_instrument(c, isin, **BOND)
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------- connector: fetch

def test_both_indices_ok_saved_one_raw_json_each(tmp_path):
    sleeps = []
    clock_value = [0.0]
    conn_, session = _connector(tmp_path, sleep=sleeps.append, clock=lambda: clock_value[0])
    recs = conn_.fetch()

    assert [r.status for r in recs] == [STATUS_OK, STATUS_OK]
    assert [r.url for r in recs] == [OTC_URL, RFQ_URL]
    assert session.urls == [OTC_URL, RFQ_URL]  # sequential, in index order
    assert sleeps and all(s >= 1.0 for s in sleeps)  # >= 1 s between requests
    for rec in recs:
        assert rec.source == "nse_corp_trades"
        assert rec.content_type == "application/json; charset=utf-8"
        assert Path(rec.raw_path).is_file()
        assert Path(rec.raw_path).parent == tmp_path / "raw" / "nse_corp_trades"
        assert rec.sha256 and rec.size_bytes > 0
    assert index_from_url(recs[0].url) == "otctrades_listed"
    assert index_from_url(recs[1].url) == "rfqtrades_listed"


def test_html_shell_with_200_is_error_and_not_saved(tmp_path):
    shell = b"<!DOCTYPE html><html><body>shell</body></html>"
    conn_, _ = _connector(tmp_path, handler=lambda url: FakeResp(200, shell, "text/html; charset=utf-8"))
    recs = conn_.fetch()
    assert all(r.status == STATUS_ERROR for r in recs)
    assert all(r.raw_path is None for r in recs)
    assert not (tmp_path / "raw" / "nse_corp_trades").exists() or not any(
        (tmp_path / "raw" / "nse_corp_trades").iterdir())


def test_wrong_content_type_with_json_body_is_error(tmp_path):
    conn_, _ = _connector(tmp_path, handler=lambda url: FakeResp(
        200, json.dumps({"data": []}).encode(), "text/plain"))
    rec = conn_.fetch()[0]
    assert rec.status == STATUS_ERROR and "not application/json" in rec.note


def test_invalid_json_is_error(tmp_path):
    conn_, _ = _connector(tmp_path, handler=lambda url: FakeResp(200, b"{not json", "application/json"))
    assert all(r.status == STATUS_ERROR and "invalid JSON" in r.note for r in conn_.fetch())


def test_json_without_data_list_is_error(tmp_path):
    conn_, _ = _connector(tmp_path, handler=lambda url: FakeResp(
        200, json.dumps({"data": {"x": 1}}).encode(), "application/json"))
    assert all(r.status == STATUS_ERROR and "'data' list" in r.note for r in conn_.fetch())


def test_non_200_is_unavailable(tmp_path):
    conn_, _ = _connector(tmp_path, handler=lambda url: FakeResp(404, b"", "text/html"))
    recs = conn_.fetch()
    assert all(r.status == STATUS_UNAVAILABLE and r.note == "HTTP 404" for r in recs)


def test_transport_error_is_unavailable_not_raised(tmp_path):
    conn_, _ = _connector(tmp_path, handler=lambda url: ConnectionError("boom"))
    recs = conn_.fetch()
    assert all(r.status == STATUS_UNAVAILABLE for r in recs)


def test_transport_error_retried_once_then_ok(tmp_path):
    calls = {"n": 0}

    def handler(url):
        if "otctrades" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                return ConnectionError("first attempt fails")
            return _json_resp(_otc_payload())
        return _json_resp(_rfq_payload())

    conn_, session = _connector(tmp_path, handler=handler)
    recs = conn_.fetch()
    assert recs[0].status == STATUS_OK
    assert session.urls.count(OTC_URL) == 2


def test_default_indices_are_the_two_listed_corporate_debt_indices():
    assert CORP_TRADES_INDICES == ("otctrades_listed", "rfqtrades_listed")


# ---------------------------------------------------------------- connector: parse

def test_parse_drops_invalid_and_sovereign_isins_and_strips_descriptor(tmp_path):
    conn_, _ = _connector(tmp_path)
    rows = conn_.parse(_otc_payload(), "otctrades_listed")
    assert [r["isin"] for r in rows] == [A, B]
    assert conn_.stats == {"rows": 4, "invalid_isin": 1, "gov_dropped": 1, "kept": 2}
    first = rows[0]
    assert first["name"] == "SYNTHETIC A SR 1 9 NCD 30OC30"  # trailing \r removed
    assert first["ltp"] == 98.5 and first["lty"] == 9.6 and first["wap"] == 98.4 and first["way"] == 9.7
    assert first["trade_value"] == 5000.0 and first["trades"] == 5
    assert first["trade_date"] == "2026-10-30"
    assert first["response_timestamp"] == "2026-10-30T17:13:00"
    assert first["index"] == "otctrades_listed" and first["source_url"] == OTC_URL


def test_timestamp_formats_and_malformed_timestamp():
    assert parse_corp_timestamp("30-Oct-2026 17:13") == ("2026-10-30T17:13:00", "2026-10-30")
    assert parse_corp_timestamp("30-Oct-2026 17:13:00") == ("2026-10-30T17:13:00", "2026-10-30")
    with pytest.raises(ValueError):
        parse_corp_timestamp("2026-10-30 17:13")
    with pytest.raises(ValueError):
        parse_corp_timestamp(None)


def test_parse_with_malformed_timestamp_raises(tmp_path):
    conn_, _ = _connector(tmp_path)
    with pytest.raises(ValueError):
        conn_.parse({"data": [], "timestamp": "yesterday"}, "otctrades_listed")


def test_non_numeric_price_becomes_none_not_zero(tmp_path):
    conn_, _ = _connector(tmp_path)
    payload = _otc_payload()
    payload["data"][1]["ltp"] = "n/a"
    rows = conn_.parse(payload, "otctrades_listed")
    b = next(r for r in rows if r["isin"] == B)
    assert b["ltp"] is None


# ---------------------------------------------------------------- duplicate ISIN choice

def _row(isin, trades, value, index, ltp=98.0, trade_date="2026-10-30"):
    return {"isin": isin, "ltp": ltp, "trades": trades, "trade_value": value, "index": index,
            "trade_date": trade_date, "source_url": corp_trades_url(index),
            "response_timestamp": "2026-10-30T17:13:00", "lty": 9.0, "wap": ltp, "way": 9.0,
            "name": "X"}


def test_duplicate_isin_prefers_more_trades_then_larger_value():
    rows = [
        _row(A, 5, 5000, "otctrades_listed", ltp=98.5),
        _row(A, 9, 8000, "rfqtrades_listed", ltp=98.7),
    ]
    selected, dropped, no_price = select_one_per_isin(rows)
    assert dropped == 1 and no_price == 0
    assert selected[0]["ltp"] == 98.7 and selected[0]["index"] == "rfqtrades_listed"

    tie = [_row(A, 5, 1000, "otctrades_listed", ltp=10.0), _row(A, 5, 2000, "rfqtrades_listed", ltp=20.0)]
    selected, _, _ = select_one_per_isin(tie)
    assert selected[0]["ltp"] == 20.0  # tie on trades -> larger trade_value


def test_rows_without_price_are_not_selected_and_are_counted():
    rows = [_row(A, 3, 100, "otctrades_listed", ltp=None), _row(B, 1, 50, "otctrades_listed")]
    selected, dropped, no_price = select_one_per_isin(rows)
    assert [r["isin"] for r in selected] == [B]
    assert no_price == 1 and dropped == 0


# ---------------------------------------------------------------- ingestion (pipeline)

def _records(url_ok=True):
    recs = []
    for url in (OTC_URL, RFQ_URL):
        recs.append(SourceRecord("nse_corp_trades", url, FETCHED, "application/json", 10,
                                 ("a" if url == OTC_URL else "b") * 64, "raw/x.json", STATUS_OK,
                                 "index=x"))
    return recs


def _all_rows(tmp_path):
    conn_, _ = _connector(tmp_path)
    return conn_.parse(_otc_payload(), "otctrades_listed") + conn_.parse(_rfq_payload(), "rfqtrades_listed")


def _snapshots(c, isin):
    return c.execute(
        "SELECT * FROM price_snapshot WHERE isin = ? AND kind = 'exchange_reported' ORDER BY id", (isin,)
    ).fetchall()


def test_first_ingest_writes_one_snapshot_per_isin_with_notes_and_no_clean_price(conn, tmp_path):
    recs = _records()
    rows = _all_rows(tmp_path)
    counts = pipeline.ingest_nse_corp_trades(conn, rows, recs)

    assert counts["rows"] == 3 and counts["selected"] == 2  # OTC: A, B; RFQ: A
    assert counts["dropped_duplicate"] == 1
    assert counts["skipped_unknown_isin"] == 0
    assert counts["new"] == 2 and counts["changed"] == 0 and counts["unchanged"] == 0
    assert counts["records_persisted"] == 2

    snap_a = _snapshots(conn, A)
    assert len(snap_a) == 1
    s = snap_a[0]
    assert s["price_per_100"] == 98.7          # RFQ row won: more trades
    assert s["reported_yield_pct"] == 9.5
    assert s["clean_price"] is None            # per-100 basis is NOT converted at ingest
    assert s["trades"] == 9 and s["value"] == 8000.0 and s["volume"] is None
    assert s["freshness"] == "delayed" and s["trade_date"] == "2026-10-30"
    assert s["observed_at"] == "2026-10-30T17:13:00"
    assert s["source_url"] == RFQ_URL
    assert s["notes"] == "wap=98.6;way=9.5;tradeValue=8000.0(unit unverified);trades=9"

    avail = conn.execute("SELECT * FROM availability WHERE isin = ?", (A,)).fetchall()
    assert len(avail) == 1
    assert avail[0]["listed"] == 1 and avail[0]["recently_traded"] == 1
    assert avail[0]["price_verified"] == 0 and avail[0]["executable_verified"] == 0


def test_coupon_pct_and_other_master_fields_untouched(conn, tmp_path):
    pipeline.ingest_nse_corp_trades(conn, _all_rows(tmp_path), _records())
    row = conn.execute("SELECT coupon_pct, face_value, coupon_hint FROM instrument WHERE isin = ?", (A,)).fetchone()
    assert row["coupon_pct"] == 9.0 and row["face_value"] == 1000.0 and row["coupon_hint"] is None


def _with_extra(isin, descriptor="SYNTHETIC NEW SR 3 9 NCD 30OC30\r"):
    payload = _otc_payload()
    payload["data"].append({"descriptor": descriptor, "isin": isin, "ltp": 99.0, "lty": 8.0,
                            "noOfTrades": 4, "tradeValue": 400, "wap": 99.0, "way": 8.0})
    return payload


def test_unknown_ine_isin_is_seeded_without_master_data(conn, tmp_path):
    conn_, _ = _connector(tmp_path)
    rows = conn_.parse(_with_extra(UNKNOWN), "otctrades_listed")
    recs = _records()[:1]
    counts = pipeline.ingest_nse_corp_trades(conn, rows, recs)
    assert counts["seeded_new_instruments"] == 1 and counts["skipped_unknown_isin"] == 0
    inst = conn.execute("SELECT * FROM instrument WHERE isin = ?", (UNKNOWN,)).fetchone()
    assert inst is not None
    assert inst["name"] == "SYNTHETIC NEW SR 3 9 NCD 30OC30"  # descriptor, trailing \r stripped
    assert inst["instrument_type"] == "DEBT" and inst["exchange_listed"] == 1
    assert inst["source_url"] == OTC_URL and inst["retrieved_on"] == "2026-10-30"
    assert inst["master_source_id"] is not None
    for field in ("face_value", "coupon_pct", "coupon_frequency", "maturity_date", "issue_date",
                  "first_interest_date", "currency", "security_type", "is_perpetual", "is_at1",
                  "is_structured", "default_flag", "trading_suspended", "coupon_hint"):
        assert inst[field] is None, field
    assert _snapshots(conn, UNKNOWN)[0]["price_per_100"] == 99.0


def test_unknown_non_ine_isin_is_skipped_never_created(conn, tmp_path):
    other = "INF000T00001"  # not an INE corporate prefix
    conn_, _ = _connector(tmp_path)
    rows = conn_.parse(_with_extra(other), "otctrades_listed")
    counts = pipeline.ingest_nse_corp_trades(conn, rows, _records()[:1])
    assert counts["skipped_unknown_isin"] == 1 and counts["seeded_new_instruments"] == 0
    assert conn.execute("SELECT 1 FROM instrument WHERE isin = ?", (other,)).fetchone() is None


def test_second_run_after_seeding_writes_nothing(conn, tmp_path):
    conn_, _ = _connector(tmp_path)
    rows = conn_.parse(_with_extra(UNKNOWN), "otctrades_listed")
    recs = _records()[:1]
    pipeline.ingest_nse_corp_trades(conn, rows, recs)
    before = conn.execute("SELECT COUNT(*) FROM instrument").fetchone()[0], \
        conn.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0]
    counts = pipeline.ingest_nse_corp_trades(conn, rows, [], existing_source_ids={OTC_URL: 1})
    assert counts["seeded_new_instruments"] == 0 and counts["new"] == 0
    assert counts["unchanged"] == 3 and counts["availability_written"] == 0
    after = conn.execute("SELECT COUNT(*) FROM instrument").fetchone()[0], \
        conn.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0]
    assert before == after


def test_seeded_instrument_is_rejected_and_never_shortlisted(tmp_path):
    c = db.connect(tmp_path / "seed_screen.sqlite")
    db.init_schema(c)
    conn_, _ = _connector(tmp_path)
    rows = conn_.parse(_with_extra(UNKNOWN), "otctrades_listed")
    pipeline.ingest_nse_corp_trades(c, rows, _records()[:1])
    run_screen(c, SETTLE, run_id="run-seed")
    row = _screen_row(c, "run-seed", UNKNOWN)
    assert row["bucket"] == "rejected" and row["ytm"] is None
    rules = {r["rule"] for r in c.execute("SELECT rule FROM rejection WHERE run_id = ? AND isin = ?",
                                          ("run-seed", UNKNOWN))}
    assert {"missing_coupon_or_schedule", "no_instrument_rating", "unverified_currency"} <= rules
    assert UNKNOWN not in set(q.shortlist(c, "run-seed", "primary")["isin"])
    assert UNKNOWN not in set(q.shortlist(c, "run-seed", "speculative")["isin"])
    c.close()


def test_second_identical_ingest_writes_nothing(conn, tmp_path):
    recs = _records()
    rows = _all_rows(tmp_path)
    pipeline.ingest_nse_corp_trades(conn, rows, recs)
    before = (conn.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM availability").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0])

    counts = pipeline.ingest_nse_corp_trades(
        conn, rows, [], existing_source_ids={OTC_URL: 1, RFQ_URL: 2})
    assert counts["new"] == 0 and counts["changed"] == 0 and counts["unchanged"] == 2
    assert counts["records_persisted"] == 0 and counts["availability_written"] == 0
    after = (conn.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM availability").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0])
    assert before == after


def test_changed_price_inserts_new_snapshot_without_new_availability(conn, tmp_path):
    pipeline.ingest_nse_corp_trades(conn, _all_rows(tmp_path), _records())
    payload = _rfq_payload()
    payload["data"][0]["ltp"] = 98.9
    rfq_rows = _connector(tmp_path)[0].parse(payload, "rfqtrades_listed")
    counts = pipeline.ingest_nse_corp_trades(conn, rfq_rows, [], existing_source_ids={RFQ_URL: 2})
    assert counts["changed"] == 1 and counts["new"] == 0
    assert counts["availability_written"] == 0  # flags unchanged
    prices = [s["price_per_100"] for s in _snapshots(conn, A)]
    assert prices == [98.7, 98.9]


def test_missing_source_record_mapping_raises_and_rolls_back(conn, tmp_path):
    with pytest.raises(ValueError):
        pipeline.ingest_nse_corp_trades(conn, _all_rows(tmp_path), [], existing_source_ids={})
    assert conn.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0] == 0


def test_failed_record_is_refused(conn, tmp_path):
    bad = SourceRecord("nse_corp_trades", OTC_URL, FETCHED, None, 0, None, None, STATUS_ERROR, "html")
    with pytest.raises(ValueError):
        pipeline.ingest_nse_corp_trades(conn, [], [bad])


# ---------------------------------------------------------------- eligibility: price conversion

def _seed_price(c, isin, *, price_per_100, trade_date="2026-10-30", reported=9.6):
    db.insert_price_snapshot(
        c, isin=isin, trade_date=trade_date, clean_price=None, volume=None, value=None, trades=3,
        kind="exchange_reported", platform=None, vendor_ytm=None, freshness="delayed",
        source_id=None, observed_at="2026-10-30T17:13:00", price_per_100=price_per_100,
        reported_yield_pct=reported,
    )


def test_per_100_converted_to_per_face_with_face_1000(conn):
    _seed_price(conn, A, price_per_100=98.5)
    q_ = latest_price(conn, A, SETTLE)
    assert q_.clean_price == pytest.approx(985.0)
    assert q_.basis == "per_100" and q_.price_per_100 == 98.5
    assert q_.reported_yield_pct == 9.6 and q_.vendor_ytm is None


def test_per_100_with_face_100_is_unchanged_numerically(conn):
    db.upsert_instrument(conn, B, face_value=100.0)
    _seed_price(conn, B, price_per_100=98.5)
    assert latest_price(conn, B, SETTLE).clean_price == pytest.approx(98.5)


def test_face_value_null_is_unusable_and_flagged_when_no_other_price(conn):
    db.upsert_instrument(conn, B, face_value=None)
    _seed_price(conn, B, price_per_100=98.5)
    q_ = latest_price(conn, B, SETTLE)
    assert q_ is not None and q_.clean_price is None and q_.price_per_100 == 98.5
    result = evaluate(conn, B, SETTLE)
    rules = [rule for rule, _ in result.failures]
    assert "unverified_face_value" in rules
    assert result.bucket == "rejected" and result.eligible is False


def test_face_value_null_uses_older_usable_price_when_available(conn):
    db.upsert_instrument(conn, B, face_value=None)
    db.insert_price_snapshot(
        conn, isin=B, trade_date="2026-10-29", clean_price=995.0, volume=None, value=None, trades=None,
        kind="exchange_close", platform=None, vendor_ytm=None, freshness="delayed", source_id=None,
        observed_at=None,
    )
    _seed_price(conn, B, price_per_100=98.5)  # newer, but unusable (face NULL)
    q_ = latest_price(conn, B, SETTLE)
    assert q_.kind == "exchange_close" and q_.clean_price == 995.0 and q_.basis == "per_face"
    assert "unverified_face_value" not in [r for r, _ in evaluate(conn, B, SETTLE).failures]


def test_latest_trade_date_wins_and_ties_go_to_larger_id(conn):
    db.insert_price_snapshot(
        conn, isin=A, trade_date="2026-10-29", clean_price=990.0, volume=None, value=None, trades=None,
        kind="exchange_close", platform=None, vendor_ytm=None, freshness="delayed", source_id=None,
        observed_at=None,
    )
    _seed_price(conn, A, price_per_100=98.5, trade_date="2026-10-30")
    assert latest_price(conn, A, SETTLE).kind == "exchange_reported"
    _seed_price(conn, A, price_per_100=99.0, trade_date="2026-10-30")
    q_ = latest_price(conn, A, SETTLE)
    assert q_.price_per_100 == 99.0  # same date: larger id wins


# ---------------------------------------------------------------- screener: mismatch flag and cap

MISMATCH_ISIN = "INE000T00030"
ISIN_OK = "INE000T00031"
ISIN_BAD = "INE000T00032"
ISIN_NULL = "INE000T00033"  # NSE reported yield missing
CLEAN_PER_100 = 98.5


def _screen_bond(c, isin, reported_offset):
    # reported_offset None -> NSE reported yield stored as NULL
    db.upsert_instrument(c, isin, **BOND)
    db.insert_rating(
        c, isin=isin, agency="CRISIL", rating="AA+/Stable", outlook=None, action="assigned",
        action_date="2026-01-15", rationale_url=None, source_id=None, source_type="manual",
        source_ref=SOURCE,
    )
    db.insert_availability(
        c, isin=isin, listed=1, recently_traded=1, offered_for_sale=0, price_verified=1,
        executable_verified=1, min_investment=None, platform=None,
        observed_at="2026-10-30T10:00:00Z", source_id=None,
    )
    _seed_price(c, isin, price_per_100=CLEAN_PER_100, trade_date="2026-10-30", reported=0.0)
    expected = compute_ytm(1000.0, 9.0, "annual", date(2030, 10, 30), SETTLE, CLEAN_PER_100 * 10.0,
                           issue_date=date(2025, 10, 30)).ytm * 100.0
    reported = None if reported_offset is None else round(expected + reported_offset, 4)
    c.execute("UPDATE price_snapshot SET reported_yield_pct = ? WHERE isin = ?", (reported, isin))


@pytest.fixture
def screen_conn(tmp_path):
    c = db.connect(tmp_path / "screen.sqlite")
    db.init_schema(c)
    _screen_bond(c, ISIN_OK, 0.20)            # within 0.25pp -> no flag
    _screen_bond(c, MISMATCH_ISIN, 0.30)      # beyond 0.25pp -> flag, confidence capped
    _screen_bond(c, ISIN_BAD, -0.30)          # beyond in the other direction
    _screen_bond(c, ISIN_NULL, None)          # no reported yield: cannot verify -> capped
    c.commit()
    yield c
    c.close()


def _screen_row(c, run_id, isin):
    return c.execute("SELECT * FROM screen_result WHERE run_id = ? AND isin = ?", (run_id, isin)).fetchone()


def test_reported_yield_mismatch_flag_threshold_and_confidence_cap(screen_conn):
    run_screen(screen_conn, SETTLE, run_id="run-m")
    ok = _screen_row(screen_conn, "run-m", ISIN_OK)
    bad = _screen_row(screen_conn, "run-m", MISMATCH_ISIN)
    neg = _screen_row(screen_conn, "run-m", ISIN_BAD)

    assert ok["confidence"] == "High" and "reported_yield_mismatch" not in (ok["flags"] or "")
    for row in (bad, neg):
        assert "reported_yield_mismatch" in row["flags"].split(",")
        assert "reported_basis_unverified" in row["flags"].split(",")
        assert row["confidence"] == "Medium"
    assert "reported_basis_unverified" not in (ok["flags"] or "")
    assert ok["price_kind"] == "exchange_reported" and ok["clean_price"] == pytest.approx(985.0)
    # NSE-reported yield is stored in the vendor slot (unverified), never used as our YTM
    assert ok["vendor_ytm"] == pytest.approx(round(ok["ytm"] * 100.0 + 0.20, 4), abs=1e-3)
    assert ok["ytm"] != pytest.approx(ok["vendor_ytm"])


def test_exchange_reported_high_needs_match_else_capped_with_basis_flag(screen_conn):
    run_screen(screen_conn, SETTLE, run_id="run-n")
    row = _screen_row(screen_conn, "run-n", ISIN_NULL)
    assert row["price_kind"] == "exchange_reported" and row["ytm"] is not None
    assert row["confidence"] == "Medium"
    flags = row["flags"].split(",")
    assert "reported_basis_unverified" in flags
    assert "reported_yield_mismatch" not in flags


def test_shortlist_shows_nse_reported_yield_separately_from_vendor(screen_conn):
    run_screen(screen_conn, SETTLE, run_id="run-q")
    df = q.shortlist(screen_conn, "run-q", "primary")
    row = df[df["isin"] == ISIN_OK].iloc[0]
    assert pd.isna(row["vendor_ytm"])  # exchange_reported never fills the vendor column
    assert row["nse_reported_yield"] == pytest.approx(
        round(row["ytm_pct"] + 0.20, 4), abs=1e-3)
    disp = q.display_frame(df)
    assert "NSE reported traded yield (unverified)" in disp.columns
    assert "NSE OTC/RFQ reported (basis unverified)" in set(disp["Price kind"])


def test_existing_vendor_ytm_is_not_overwritten_by_reported_yield(screen_conn):
    # A marketplace vendor YTM already in the slot wins; the NSE figure does not replace it.
    db.insert_price_snapshot(
        screen_conn, isin=ISIN_OK, trade_date="2026-10-30", clean_price=980.0, volume=None, value=None,
        trades=None, kind="marketplace", platform="SYNTHETIC", vendor_ytm=11.1, freshness="indicative",
        source_id=None, observed_at="2026-10-30T23:59:00Z",
    )
    run_screen(screen_conn, SETTLE, run_id="run-v")
    row = _screen_row(screen_conn, "run-v", ISIN_OK)
    assert row["price_kind"] == "marketplace" and row["vendor_ytm"] == 11.1


# ---------------------------------------------------------------- schema and migration

OLD_PRICE_SNAPSHOT = """
CREATE TABLE price_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    isin        TEXT NOT NULL REFERENCES instrument(isin),
    trade_date  TEXT,
    clean_price REAL,
    volume      REAL,
    value       REAL,
    trades      INTEGER,
    kind        TEXT NOT NULL CHECK (kind IN ('exchange_close', 'marketplace')),
    platform    TEXT,
    vendor_ytm  REAL,
    freshness   TEXT CHECK (freshness IN ('live', 'delayed', 'stale', 'indicative')),
    source_id   INTEGER REFERENCES source_record(id),
    observed_at TEXT,
    source_url  TEXT,
    notes       TEXT
);
"""


def test_fresh_schema_has_new_columns_and_accepts_exchange_reported(tmp_path):
    c = db.connect(tmp_path / "fresh.sqlite")
    db.init_schema(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(price_snapshot)")}
    assert {"price_per_100", "reported_yield_pct"} <= cols
    db.upsert_instrument(c, A, **BOND)
    _seed_price(c, A, price_per_100=98.5)
    c.commit()
    c.close()


def test_old_db_gets_columns_by_alter_and_new_kind_raises_clear_error(tmp_path):
    path = tmp_path / "old.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_PRICE_SNAPSHOT)
    raw.execute("CREATE TABLE instrument (isin TEXT PRIMARY KEY)")
    raw.execute("INSERT INTO instrument (isin) VALUES (?)", (A,))
    raw.execute("INSERT INTO price_snapshot (isin, trade_date, clean_price, kind) "
                "VALUES (?, '2026-10-29', 990.0, 'exchange_close')", (A,))
    raw.commit()
    raw.close()

    c = db.connect(path)
    db.init_schema(c)  # must not fail; adds the two columns
    cols = {r[1] for r in c.execute("PRAGMA table_info(price_snapshot)")}
    assert {"price_per_100", "reported_yield_pct"} <= cols
    # existing rows and the old kind keep working
    assert c.execute("SELECT clean_price FROM price_snapshot").fetchone()[0] == 990.0
    db.insert_price_snapshot(
        c, isin=A, trade_date="2026-10-30", clean_price=991.0, volume=None, value=None, trades=None,
        kind="exchange_close", platform=None, vendor_ytm=None, freshness="delayed", source_id=None,
        observed_at=None,
    )
    # the old CHECK cannot be altered: exchange_reported must fail with a rebuild message
    with pytest.raises(db.SchemaRebuildRequired, match="Delete the database file"):
        db.insert_price_snapshot(
            c, isin=A, trade_date="2026-10-30", clean_price=None, volume=None, value=None, trades=None,
            kind="exchange_reported", platform=None, vendor_ytm=None, freshness="delayed",
            source_id=None, observed_at=None, price_per_100=98.5, reported_yield_pct=9.0,
        )
    c.rollback()
    c.close()


def test_init_schema_is_idempotent_on_migrated_db(tmp_path):
    c = db.connect(tmp_path / "idem.sqlite")
    db.init_schema(c)
    db.init_schema(c)
    c.close()


# ---------------------------------------------------------------- refresh wiring

class Unavailable:
    def __init__(self, source, url):
        self.source = source
        self.url = url

    def fetch(self):
        return SourceRecord(self.source, self.url, FETCHED, None, 0, None, None,
                            STATUS_UNAVAILABLE, "simulated outage")


def _refresh(tmp_path, *, corp_conn=None, corp_trades=True, settlement=SETTLE):
    from india_bonds.data_sources.nse import NseCmBhavcopy, NseDebtUniverse, URL_UNIVERSE
    from india_bonds.data_sources.nse import bhav_url
    connectors = {
        "universe": Unavailable(NseDebtUniverse.source, URL_UNIVERSE),
        "bhavcopy": Unavailable(NseCmBhavcopy.source, bhav_url(settlement)),
    }
    if corp_conn is not None:
        connectors["corp_trades"] = corp_conn
    return run_refresh(
        tmp_path / "data" / "bonds.db", settlement, False, tmp_path / "inputs", tmp_path / "reports",
        make_report=False, connectors=connectors, corp_trades=corp_trades,
    )


def _seed_db(tmp_path):
    path = tmp_path / "data" / "bonds.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    c = db.connect(path)
    db.init_schema(c)
    for isin in (A, B):
        db.upsert_instrument(c, isin, **BOND)
    c.commit()
    c.close()


TABLES = ("price_snapshot", "availability", "source_record", "screen_result")


def _table_counts(tmp_path):
    c = sqlite3.connect(tmp_path / "data" / "bonds.db")
    out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    c.close()
    return out


def test_refresh_first_run_ingests_second_identical_run_writes_nothing(tmp_path):
    _seed_db(tmp_path)
    conn_, _ = _connector(tmp_path)
    s1 = _refresh(tmp_path, corp_conn=conn_)
    assert [src.name for src in s1.sources if src.name.startswith("nse_corp_trades")] == [
        "nse_corp_trades (otctrades_listed)", "nse_corp_trades (rfqtrades_listed)"]
    assert s1.corp_rows == 3 and s1.corp_new == 2 and s1.corp_changed == 0
    assert s1.corp_skipped_unknown_isin == 0
    assert s1.corp_trade_date == "2026-10-30"
    counts1 = _table_counts(tmp_path)
    assert counts1["price_snapshot"] >= 2

    line = format_summary(s1)
    assert "corp-trades (OTC/RFQ): rows=3 new=2 changed=0 unchanged=0 skipped_unknown_isin=0" in line
    assert not any("older than 4 days" in w for w in s1.warnings)

    conn2, _ = _connector(tmp_path)
    s2 = _refresh(tmp_path, corp_conn=conn2)
    assert s2.corp_new == 0 and s2.corp_changed == 0 and s2.corp_unchanged == 2
    assert s2.screen_reused is True
    assert "DB unchanged" in format_summary(s2)
    assert _table_counts(tmp_path) == counts1


def test_refresh_no_corp_trades_flag_never_fetches(tmp_path):
    _seed_db(tmp_path)
    conn_, session = _connector(tmp_path)
    s = _refresh(tmp_path, corp_conn=conn_, corp_trades=False)
    assert session.urls == []
    statuses = {src.name: src.status for src in s.sources}
    assert statuses[NseCorpBondTrades.source] == "skipped"
    assert s.corp_rows is None
    assert "corp-trades (OTC/RFQ):" not in format_summary(s)


def test_refresh_stale_corp_response_warns(tmp_path):
    _seed_db(tmp_path)
    conn_, _ = _connector(tmp_path)
    s = _refresh(tmp_path, corp_conn=conn_, settlement=date(2026, 11, 5))  # 6 days after response
    assert any(w.startswith("corp-trades: newest NSE response timestamp date 2026-10-30 is 6 day(s)")
               for w in s.warnings)


def test_refresh_unavailable_corp_index_is_warning_not_crash(tmp_path):
    _seed_db(tmp_path)
    conn_, _ = _connector(tmp_path, handler=lambda url: FakeResp(500, b"", "text/html"))
    s = _refresh(tmp_path, corp_conn=conn_)
    assert any(src.status == "unavailable" for src in s.sources)
    assert any("nse_corp_trades (otctrades_listed) unavailable" in w for w in s.warnings)


def test_refresh_refuses_to_write_when_schema_is_old(tmp_path):
    from india_bonds.database.db import SchemaRebuildRequired as Err
    path = tmp_path / "data" / "bonds.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    raw.executescript(OLD_PRICE_SNAPSHOT)
    raw.execute("CREATE TABLE instrument (isin TEXT PRIMARY KEY)")
    raw.execute("INSERT INTO instrument (isin) VALUES (?)", (A,))
    raw.commit()
    raw.close()
    conn_, _ = _connector(tmp_path)
    with pytest.raises(Err):
        _refresh(tmp_path, corp_conn=conn_)


# ---------------------------------------------------------------- reports

def test_report_and_dashboard_labels():
    assert q.price_kind_label("exchange_reported") == "NSE OTC/RFQ reported (basis unverified)"
    assert q.price_kind_label("exchange_close") == "exchange_close"
    assert q.NSE_REPORTED_YIELD_LABEL == "NSE reported traded yield (unverified)"
    assert "reported_yield_mismatch" in md_report.FLAG_EXPLAIN
    assert "0.25 percentage points" in md_report.FLAG_EXPLAIN["reported_yield_mismatch"]
    assert "NSE reported traded yield (unverified) (%)" in md_report.SHORTLIST_HEADERS
    assert "nse_reported_yield_pct" in md_report.SHORTLIST_CSV_COLUMNS


def test_full_report_renders_nse_labels_and_mismatch_flag(screen_conn, tmp_path):
    from india_bonds.reports.markdown import write_report
    run_screen(screen_conn, SETTLE, run_id="screen-2026-10-30")
    result = write_report(screen_conn, tmp_path / "reports", run_id="screen-2026-10-30")
    day = Path(result.day_dir)
    shortlist = (day / "shortlist.csv").read_text(encoding="utf-8")
    assert "nse_reported_yield_pct" in shortlist.splitlines()[0]
    assert "reported_yield_mismatch" in shortlist
    page = (day / "bonds" / f"{MISMATCH_ISIN}.md").read_text(encoding="utf-8")
    assert "NSE reported traded yield (unverified) (%)" in page
    assert "NSE OTC/RFQ reported (basis unverified)" in page
    assert "`reported_yield_mismatch`" in page
