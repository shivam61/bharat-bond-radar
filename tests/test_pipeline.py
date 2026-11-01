"""Tests for india_bonds.pipeline. Uses the NSE excerpt fixtures and SYNTHETIC rows only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from india_bonds import pipeline
from india_bonds.data_sources.base import STATUS_ERROR, STATUS_OK, STATUS_UNAVAILABLE, SourceRecord
from india_bonds.data_sources.nse import NseCmBhavcopy, NseDebtUniverse, csv_rows_from_bytes
from india_bonds.database import db
from india_bonds.screening.eligibility import latest_price

FIXTURES = Path(__file__).parent / "fixtures"
UNIVERSE_PATH = FIXTURES / "nse_active_debt_excerpt.csv"
BHAV_BYTES = (FIXTURES / "nse_bhav_excerpt.csv").read_bytes()

UNIVERSE_URL = "https://nsearchives.nseindia.com/content/equities/List_of_Active_Securities_CM_DEBT.csv"
BHAV_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv.zip"
FETCHED = "2026-11-01T18:16:14+00:00"

# Real NSE rows from the excerpt (public data, not vendor/advice content).
AFIL = "INE916Y07057"   # N0 series, name 'AFIL 12% 2028'
ECL = "INE528S07110"    # N1 series, name 'ECL 8.88% 2028 SR V'
SCL_HINT = "INE148I07YI2"  # name 'SCL 8.7923% 2030 SR I' -> hint 8.7923
ADITYA = "INE831R07359"  # DEBT, no coupon hint in name
GOV = "IN0020170059"     # sovereign, dropped by corporate_only heuristic


def make_record(url=UNIVERSE_URL, status=STATUS_OK, source="nse_debt_universe", fetched_at=FETCHED):
    return SourceRecord(
        source=source,
        url=url,
        fetched_at=fetched_at,
        content_type="text/csv",
        size_bytes=123,
        sha256="0" * 64,
        raw_path="raw/synthetic_test_payload.csv" if status == STATUS_OK else None,
        status=status,
        note=None if status == STATUS_OK else "unavailable for test",
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "pipeline.sqlite")
    db.init_schema(c)
    yield c
    c.close()


def universe_rows():
    return NseDebtUniverse().parse_file(UNIVERSE_PATH)


def bhav_rows():
    return NseCmBhavcopy(date(2026, 10, 30)).parse_rows(csv_rows_from_bytes(BHAV_BYTES))


def instrument(conn, isin):
    return conn.execute("SELECT * FROM instrument WHERE isin = ?", (isin,)).fetchone()


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------- persist_source_record ----------

def test_persist_source_record_maps_all_fields(conn):
    rec = SourceRecord(
        source="nse_cm_bhavcopy", url=BHAV_URL, fetched_at=FETCHED, content_type="application/zip",
        size_bytes=2048, sha256="ab" * 32, raw_path="/data/raw/x.zip", status=STATUS_OK,
        note="trade_date=2026-10-30",
    )
    sid = pipeline.persist_source_record(conn, rec)
    row = conn.execute("SELECT * FROM source_record WHERE id = ?", (sid,)).fetchone()
    assert row["source"] == "nse_cm_bhavcopy"
    assert row["url"] == BHAV_URL
    assert row["fetched_at"] == FETCHED
    assert row["content_type"] == "application/zip"
    assert row["size_bytes"] == 2048
    assert row["sha256"] == "ab" * 32
    assert row["raw_path"] == "/data/raw/x.zip"
    assert row["status"] == STATUS_OK
    assert row["note"] == "trade_date=2026-10-30"


def test_persist_source_record_keeps_failed_status(conn):
    rec = make_record(status=STATUS_UNAVAILABLE)
    sid = pipeline.persist_source_record(conn, rec)
    assert conn.execute("SELECT status FROM source_record WHERE id = ?", (sid,)).fetchone()[0] == STATUS_UNAVAILABLE


# ---------- ingest_nse_universe ----------

def test_universe_creates_listed_instruments_with_only_listing_fields(conn):
    rows = universe_rows()
    result = pipeline.ingest_nse_universe(conn, rows, make_record())

    assert result["instruments"] == len(rows) == count(conn, "instrument")
    inst = instrument(conn, ADITYA)
    assert inst["name"] == "ADITYA BIRLA HOUSING FINANCE LIMITED"
    assert inst["instrument_type"] == "DEBT"
    assert inst["exchange_listed"] == 1
    assert inst["source_url"] == UNIVERSE_URL
    assert inst["retrieved_on"] == "2026-11-01"
    assert inst["master_source_id"] == result["source_id"]
    # Nothing else is defaulted from the universe file.
    for col in ("face_value", "coupon_pct", "coupon_frequency", "first_interest_date",
                "maturity_date", "issue_date", "security_type", "seniority", "collateral_text",
                "is_perpetual", "is_at1", "is_structured", "default_flag", "trading_suspended",
                "currency", "coupon_hint", "issuer_id"):
        assert inst[col] is None, col


def test_universe_persists_source_record_and_links_it(conn):
    result = pipeline.ingest_nse_universe(conn, universe_rows(), make_record())
    src = conn.execute("SELECT * FROM source_record WHERE id = ?", (result["source_id"],)).fetchone()
    assert src["source"] == "nse_debt_universe"
    assert src["status"] == STATUS_OK
    assert src["fetched_at"] == FETCHED


def test_universe_excludes_sovereign_and_hybrid_rows(conn):
    pipeline.ingest_nse_universe(conn, universe_rows(), make_record())
    assert instrument(conn, GOV) is None
    assert instrument(conn, "INE0FDU25010") is None  # Hybrid trust row, filtered by parser


def test_universe_rerun_is_idempotent_and_keeps_master_fields(conn):
    pipeline.ingest_nse_universe(conn, universe_rows(), make_record())
    db.upsert_instrument(conn, ADITYA, coupon_pct=7.5, maturity_date="2030-01-31", currency="INR")
    conn.commit()

    pipeline.ingest_nse_universe(conn, universe_rows(), make_record())

    assert count(conn, "instrument") == len(universe_rows())
    inst = instrument(conn, ADITYA)
    assert inst["coupon_pct"] == 7.5
    assert inst["maturity_date"] == "2030-01-31"
    assert inst["currency"] == "INR"


def test_universe_refuses_non_ok_record_without_writing(conn):
    with pytest.raises(ValueError):
        pipeline.ingest_nse_universe(conn, universe_rows(), make_record(status=STATUS_UNAVAILABLE))
    assert count(conn, "instrument") == 0
    assert count(conn, "source_record") == 0


def test_universe_empty_name_does_not_blank_existing_name(conn):
    pipeline.ingest_nse_universe(conn, [{"isin": ADITYA, "name": "KEEP ME", "instrument_type": "DEBT"}],
                                 make_record())
    pipeline.ingest_nse_universe(conn, [{"isin": ADITYA, "name": "", "instrument_type": "DEBT"}],
                                 make_record())
    assert instrument(conn, ADITYA)["name"] == "KEEP ME"


# ---------- ingest_nse_bhavcopy ----------

def seed_known(conn):
    """Make ISINs present in instrument without any master data (as the universe would)."""
    db.upsert_instrument(conn, AFIL, name="AFIL 12% 2028", exchange_listed=1)
    db.upsert_instrument(conn, ECL, name="ECL 8.88% 2028 SR V", exchange_listed=1, coupon_pct=9.10)
    conn.commit()


def test_bhavcopy_inserts_exchange_close_snapshot_and_availability(conn):
    seed_known(conn)
    result = pipeline.ingest_nse_bhavcopy(conn, bhav_rows(), make_record(url=BHAV_URL,
                                                                          source="nse_cm_bhavcopy"))
    assert result["inserted"] >= 2

    snap = conn.execute(
        "SELECT * FROM price_snapshot WHERE isin = ? AND kind = 'exchange_close'", (AFIL,)
    ).fetchone()
    assert snap["trade_date"] == "2026-10-30"
    assert snap["clean_price"] == pytest.approx(9975.38)
    assert snap["volume"] == pytest.approx(60.0)
    assert snap["value"] == pytest.approx(597942.40)
    assert snap["trades"] == 6
    assert snap["freshness"] == "delayed"
    assert snap["platform"] is None
    assert snap["vendor_ytm"] is None
    assert snap["source_url"] == BHAV_URL
    assert snap["source_id"] == result["source_id"]

    av = conn.execute("SELECT * FROM availability WHERE isin = ?", (AFIL,)).fetchone()
    assert av["listed"] == 1
    assert av["recently_traded"] == 1  # volume 60 > 0
    assert av["price_verified"] == 0
    assert av["executable_verified"] == 0
    assert av["offered_for_sale"] is None


def test_bhavcopy_stores_coupon_hint_but_never_touches_coupon_pct(conn):
    seed_known(conn)
    pipeline.ingest_nse_bhavcopy(conn, bhav_rows(), make_record(url=BHAV_URL, source="nse_cm_bhavcopy"))
    afil = instrument(conn, AFIL)
    assert afil["coupon_hint"] == pytest.approx(12.0)
    assert afil["coupon_pct"] is None
    ecl = instrument(conn, ECL)
    assert ecl["coupon_hint"] == pytest.approx(8.88)
    assert ecl["coupon_pct"] == pytest.approx(9.10)  # pre-existing verified value untouched


def test_bhavcopy_skips_unknown_isin_and_never_creates_instruments(conn):
    seed_known(conn)
    before = count(conn, "instrument")
    result = pipeline.ingest_nse_bhavcopy(conn, bhav_rows(), make_record(url=BHAV_URL, source="nse_cm_bhavcopy"))
    assert result["skipped_unknown_isin"] > 0
    assert count(conn, "instrument") == before
    assert instrument(conn, SCL_HINT) is None  # present in bhavcopy, not in instrument


def test_bhavcopy_is_idempotent_for_same_isin_trade_date_kind(conn):
    seed_known(conn)
    rec = make_record(url=BHAV_URL, source="nse_cm_bhavcopy")
    first = pipeline.ingest_nse_bhavcopy(conn, bhav_rows(), rec)
    snaps_before = count(conn, "price_snapshot")
    avail_before = count(conn, "availability")

    second = pipeline.ingest_nse_bhavcopy(conn, bhav_rows(), rec)

    assert second["inserted"] == 0
    assert second["skipped_duplicate"] == first["inserted"]
    assert count(conn, "price_snapshot") == snaps_before
    assert count(conn, "availability") == avail_before


def test_bhavcopy_zero_volume_is_not_recently_traded(conn):
    db.upsert_instrument(conn, ADITYA, exchange_listed=1)
    conn.commit()
    rows = [{"isin": ADITYA, "trade_date": "2026-10-30", "close": 1000.0, "volume": 0.0,
             "value": 0.0, "trades": 0, "coupon_hint": None}]
    pipeline.ingest_nse_bhavcopy(conn, rows, make_record(url=BHAV_URL, source="nse_cm_bhavcopy"))
    av = conn.execute("SELECT recently_traded FROM availability WHERE isin = ?", (ADITYA,)).fetchone()
    assert av["recently_traded"] == 0


def test_bhavcopy_new_trade_date_adds_a_snapshot(conn):
    db.upsert_instrument(conn, ADITYA, exchange_listed=1)
    conn.commit()
    rec = make_record(url=BHAV_URL, source="nse_cm_bhavcopy")
    day = lambda d, px: {"isin": ADITYA, "trade_date": d, "close": px, "volume": 5.0,
                         "value": 5000.0, "trades": 1, "coupon_hint": None}
    pipeline.ingest_nse_bhavcopy(conn, [day("2026-10-29", 999.0)], rec)
    pipeline.ingest_nse_bhavcopy(conn, [day("2026-10-30", 998.5)], rec)
    assert count(conn, "price_snapshot") == 2


def test_bhavcopy_skips_rows_without_close_or_trade_date(conn):
    db.upsert_instrument(conn, ADITYA, exchange_listed=1)
    conn.commit()
    rows = [
        {"isin": ADITYA, "trade_date": "2026-10-30", "close": None, "volume": 1.0,
         "value": 1.0, "trades": 1, "coupon_hint": 7.0},
        {"isin": ADITYA, "trade_date": None, "close": 1000.0, "volume": 1.0,
         "value": 1.0, "trades": 1, "coupon_hint": None},
    ]
    result = pipeline.ingest_nse_bhavcopy(conn, rows, make_record(url=BHAV_URL, source="nse_cm_bhavcopy"))
    assert result["inserted"] == 0
    assert result["skipped_no_price"] == 2
    assert count(conn, "price_snapshot") == 0
    assert instrument(conn, ADITYA)["coupon_hint"] == pytest.approx(7.0)  # hint still stored


def test_bhavcopy_refuses_non_ok_record_without_writing(conn):
    seed_known(conn)
    with pytest.raises(ValueError):
        pipeline.ingest_nse_bhavcopy(conn, bhav_rows(),
                                     make_record(url=BHAV_URL, status=STATUS_ERROR, source="nse_cm_bhavcopy"))
    assert count(conn, "price_snapshot") == 0
    assert count(conn, "availability") == 0


def test_bhavcopy_snapshot_is_visible_to_screener_price_lookup(conn):
    seed_known(conn)
    pipeline.ingest_nse_bhavcopy(conn, bhav_rows(), make_record(url=BHAV_URL, source="nse_cm_bhavcopy"))
    quote = latest_price(conn, AFIL, date(2026, 10, 30))
    assert quote is not None
    assert quote.kind == "exchange_close"
    assert quote.clean_price == pytest.approx(9975.38)
    assert quote.vendor_ytm is None
