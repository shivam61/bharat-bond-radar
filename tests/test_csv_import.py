import re
from pathlib import Path

import pytest

from india_bonds.data_sources import csv_import as ci
from india_bonds.database import db

FIXTURES = Path(__file__).parent / "fixtures"
MASTER_FIX = FIXTURES / "sample_instrument_master.csv"
RATINGS_FIX = FIXTURES / "sample_ratings.csv"
MARKET_FIX = FIXTURES / "sample_marketplace.csv"

MASTER_LINE = (
    "INE000T00101,SYNTHETIC ISSUER K,SYNTHETIC K,1000,9.00,annual,2026-03-15,"
    "2028-03-15,2025-03-15,secured,senior,SYNTHETIC collateral,false,false,false,"
    "false,false,INR,SYNTHETIC TEST DATA,2026-10-31"
)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    yield c
    c.close()


def _write(tmp_path, name, header, rows):
    p = tmp_path / name
    lines = [",".join(header)] + rows
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _master_file(tmp_path, rows):
    return _write(tmp_path, "master.csv", ci.MASTER_HEADER, rows)


def _ratings_file(tmp_path, rows, name="ratings.csv"):
    return _write(tmp_path, name, ci.RATINGS_HEADER, rows)


def _market_file(tmp_path, rows, name="market.csv"):
    return _write(tmp_path, name, ci.MARKETPLACE_HEADER, rows)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------- fixtures are synthetic and valid ----------

def test_fixtures_are_synthetic_and_isins_valid():
    for path in (MASTER_FIX, RATINGS_FIX, MARKET_FIX):
        text = path.read_text(encoding="utf-8")
        assert "SYNTHETIC TEST DATA" in text
        for isin in re.findall(r"\bIN[A-Z0-9]{10}\b", text):
            assert isin.startswith("INE000T000"), isin


def test_fixture_chain_imports_cleanly(conn):
    m = ci.import_instrument_master(conn, MASTER_FIX)
    r = ci.import_ratings(conn, RATINGS_FIX)
    k = ci.import_marketplace(conn, MARKET_FIX)
    assert (m.rows_read, m.rows_ok, m.errors) == (3, 3, [])
    assert (r.rows_read, r.rows_ok, r.errors) == (3, 3, [])
    assert (k.rows_read, k.rows_ok, k.errors) == (3, 3, [])
    assert _count(conn, "instrument") == 3
    assert _count(conn, "rating") == 3
    assert _count(conn, "source_record") == 3


# ---------- header validation ----------

def test_header_mismatch_rejects_whole_file(tmp_path, conn):
    bad_header = list(ci.MASTER_HEADER)
    bad_header[3] = "face"  # renamed column
    p = _write(tmp_path, "bad.csv", bad_header, [MASTER_LINE])
    rep = ci.import_instrument_master(conn, p)
    assert rep.rows_read == 0 and rep.rows_ok == 0
    assert len(rep.errors) == 1 and rep.errors[0][0] == 1
    assert "header mismatch" in rep.errors[0][1]
    assert _count(conn, "instrument") == 0
    row = conn.execute("SELECT status FROM source_record WHERE id=?", (rep.source_record_id,)).fetchone()
    assert row["status"] == "error"


def test_header_column_order_must_match(tmp_path, conn):
    swapped = list(ci.RATINGS_HEADER)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    p = _write(tmp_path, "r.csv", swapped, [])
    rep = ci.import_ratings(conn, p)
    assert rep.errors and "header mismatch" in rep.errors[0][1]


def test_marketplace_header_mismatch(tmp_path, conn):
    p = _write(tmp_path, "m.csv", ci.MASTER_HEADER, [])
    rep = ci.import_marketplace(conn, p)
    assert rep.errors and "header mismatch" in rep.errors[0][1]


# ---------- row validation ----------

def test_bad_isin_is_row_error_and_good_rows_still_load(tmp_path, conn):
    bad = MASTER_LINE.replace("INE000T00101", "XX000T00101", 1)
    short = MASTER_LINE.replace("INE000T00101", "INE000T001", 1)
    lower = MASTER_LINE.replace("INE000T00101", "ine000t00101", 1)
    p = _master_file(tmp_path, [bad, short, lower, MASTER_LINE])
    rep = ci.import_instrument_master(conn, p)
    assert rep.rows_read == 4
    assert rep.rows_ok == 1
    assert [n for n, _ in rep.errors] == [2, 3, 4]
    assert all("isin" in msg for _, msg in rep.errors)
    assert _count(conn, "instrument") == 1


def test_bad_maturity_date_is_row_error(tmp_path, conn):
    bad_fmt = MASTER_LINE.replace("2028-03-15", "15/03/2028")
    bad_cal = MASTER_LINE.replace("2028-03-15", "2028-02-30")
    p = _master_file(tmp_path, [bad_fmt, bad_cal])
    rep = ci.import_instrument_master(conn, p)
    assert rep.rows_ok == 0
    assert len(rep.errors) == 2
    assert "maturity_date" in rep.errors[0][1]
    assert "maturity_date" in rep.errors[1][1]
    assert _count(conn, "instrument") == 0


def test_invalid_enum_and_boolean_are_row_errors(tmp_path, conn):
    bad_freq = MASTER_LINE.replace("annual", "weekly")
    bad_bool = MASTER_LINE.replace("false,false,false,false,false,INR", "yes,false,false,false,false,INR", 1)
    p = _master_file(tmp_path, [bad_freq, bad_bool])
    rep = ci.import_instrument_master(conn, p)
    assert rep.rows_ok == 0
    assert "coupon_frequency" in rep.errors[0][1]
    assert "is_perpetual" in rep.errors[1][1]


def test_wrong_cell_count_is_row_error(tmp_path, conn):
    p = _master_file(tmp_path, [MASTER_LINE + ",extra"])
    rep = ci.import_instrument_master(conn, p)
    assert rep.rows_ok == 0
    assert "cells" in rep.errors[0][1]


def test_blank_cells_become_null_never_defaulted(tmp_path, conn):
    cells = [""] * len(ci.MASTER_HEADER)
    cells[0] = "INE000T00102"
    cells[ci.MASTER_HEADER.index("maturity_date")] = "2029-06-30"
    blank = ",".join(cells)
    p = _master_file(tmp_path, [blank])
    rep = ci.import_instrument_master(conn, p)
    assert rep.rows_ok == 1 and rep.errors == []
    row = conn.execute("SELECT * FROM instrument WHERE isin='INE000T00102'").fetchone()
    for col in ("issuer_id", "name", "face_value", "coupon_pct", "coupon_frequency",
                "first_interest_date", "issue_date", "security_type", "seniority",
                "collateral_text", "is_perpetual", "is_at1", "is_structured",
                "default_flag", "trading_suspended", "currency"):
        assert row[col] is None, col
    assert row["maturity_date"] == "2029-06-30"


def test_boolean_true_false_stored_as_int(tmp_path, conn):
    line = MASTER_LINE.replace("false,false,false,false,false,INR", "true,false,,false,true,INR", 1)
    p = _master_file(tmp_path, [line.replace("INE000T00101", "INE000T00103")])
    assert ci.import_instrument_master(conn, p).rows_ok == 1
    row = conn.execute("SELECT * FROM instrument WHERE isin='INE000T00103'").fetchone()
    assert (row["is_perpetual"], row["is_at1"], row["is_structured"],
            row["default_flag"], row["trading_suspended"]) == (1, 0, None, 0, 1)


def test_master_upsert_and_idempotent_reimport(tmp_path, conn):
    p = _master_file(tmp_path, [MASTER_LINE])
    first = ci.import_instrument_master(conn, p)
    second = ci.import_instrument_master(conn, p)
    assert first.rows_ok == 1 and second.rows_ok == 1
    assert _count(conn, "instrument") == 1
    assert _count(conn, "issuer") == 1
    assert _count(conn, "source_record") == 2
    assert second.source_record_id != first.source_record_id


def test_master_reimport_updates_changed_values(tmp_path, conn):
    ci.import_instrument_master(conn, _master_file(tmp_path, [MASTER_LINE]))
    changed = MASTER_LINE.replace(",9.00,", ",9.25,", 1)
    ci.import_instrument_master(conn, _write(tmp_path, "master2.csv", ci.MASTER_HEADER, [changed]))
    assert conn.execute("SELECT coupon_pct FROM instrument WHERE isin='INE000T00101'").fetchone()[0] == 9.25


def test_source_record_written_with_sha256(tmp_path, conn):
    import hashlib

    p = _master_file(tmp_path, [MASTER_LINE])
    rep = ci.import_instrument_master(conn, p)
    row = conn.execute("SELECT * FROM source_record WHERE id=?", (rep.source_record_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert row["source"] == "csv:instrument_master"


# ---------- ratings ----------

def _instrument_row(tmp_path, conn):
    ci.import_instrument_master(conn, _master_file(tmp_path, [MASTER_LINE]))


RATING_ROW = (
    "{isin},SYNTHETIC ISSUER K,SYNTHETIC K,NCD,SYNTHETIC AGENCY X,{rating},stable,"
    "{action},{date},,manual,SYNTHETIC TEST DATA,2026-10-31"
)


def test_duplicate_rating_is_counted_not_error(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    row = RATING_ROW.format(isin="INE000T00101", rating="AA", action="assigned", date="2025-01-10")
    p = _ratings_file(tmp_path, [row, row])
    rep = ci.import_ratings(conn, p)
    assert rep.errors == []
    assert rep.rows_ok == 1
    assert rep.duplicates == 1
    assert _count(conn, "rating") == 1


def test_rating_reimport_is_idempotent(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    row = RATING_ROW.format(isin="INE000T00101", rating="AA", action="assigned", date="2025-01-10")
    p = _ratings_file(tmp_path, [row])
    ci.import_ratings(conn, p)
    rep = ci.import_ratings(conn, p)
    assert rep.rows_ok == 0 and rep.duplicates == 1 and rep.errors == []
    assert _count(conn, "rating") == 1


def test_unknown_isin_rating_is_row_error_and_creates_no_instrument(tmp_path, conn):
    row = RATING_ROW.format(isin="INE000T00777", rating="AA", action="assigned", date="2025-01-10")
    rep = ci.import_ratings(conn, _ratings_file(tmp_path, [row]))
    assert rep.rows_ok == 0
    assert rep.errors == [(2, "unknown isin, import instrument master first")]
    assert _count(conn, "instrument") == 0
    assert _count(conn, "rating") == 0


def test_rating_bad_action_and_date_are_row_errors(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    bad_action = RATING_ROW.format(isin="INE000T00101", rating="AA", action="raised", date="2025-01-10")
    bad_date = RATING_ROW.format(isin="INE000T00101", rating="AA", action="assigned", date="10-01-2025")
    blank_date = RATING_ROW.format(isin="INE000T00101", rating="AA", action="assigned", date="")
    rep = ci.import_ratings(conn, _ratings_file(tmp_path, [bad_action, bad_date, blank_date]))
    assert rep.rows_ok == 0
    assert "rating_action" in rep.errors[0][1]
    assert "action_date" in rep.errors[1][1]
    assert "action_date" in rep.errors[2][1]


def test_rating_is_instrument_level_keyed_by_isin(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    row = RATING_ROW.format(isin="INE000T00101", rating="AA", action="assigned", date="2025-01-10")
    ci.import_ratings(conn, _ratings_file(tmp_path, [row]))
    r = conn.execute("SELECT isin, agency, rating FROM rating").fetchone()
    assert (r["isin"], r["agency"], r["rating"]) == ("INE000T00101", "SYNTHETIC AGENCY X", "AA")
    assert "issuer_id" not in r.keys()


# ---------- marketplace ----------

MKT_HEAD = "INE000T00101,SYNTHETIC PLATFORM P,,,1000,{price},{ytm},,,,10000,{avail},,,{obs},SYNTHETIC TEST DATA,SYNTHETIC TEST DATA"


def _mkt_row(price="985.50", ytm="9.80", avail="open", obs="2026-10-31"):
    return MKT_HEAD.format(price=price, ytm=ytm, avail=avail, obs=obs)


def test_marketplace_writes_snapshot_with_vendor_ytm_separate(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    rep = ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row()]))
    assert rep.rows_ok == 1 and rep.errors == []
    snap = conn.execute("SELECT * FROM price_snapshot").fetchone()
    assert snap["kind"] == "marketplace"
    assert snap["clean_price"] == 985.50
    assert snap["vendor_ytm"] == 9.80
    assert snap["freshness"] == "indicative"
    assert snap["platform"] == "SYNTHETIC PLATFORM P"
    assert snap["observed_at"] == "2026-10-31"
    assert snap["trade_date"] is None
    # vendor YTM never written to instrument master
    assert "vendor_ytm" not in conn.execute("SELECT * FROM instrument").fetchone().keys()


@pytest.mark.parametrize(
    "avail,offered",
    [("open", 1), ("closed", 0), ("sold_out", 0), ("unknown", 0), ("", None)],
)
def test_marketplace_availability_flags(tmp_path, conn, avail, offered):
    _instrument_row(tmp_path, conn)
    ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row(avail=avail)]))
    a = conn.execute("SELECT * FROM availability").fetchone()
    assert a["offered_for_sale"] == offered
    assert a["price_verified"] == 0
    assert a["executable_verified"] == 0
    assert a["listed"] is None
    assert a["min_investment"] == 10000.0


def test_marketplace_bad_availability_is_row_error(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    rep = ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row(avail="maybe")]))
    assert rep.rows_ok == 0
    assert "availability" in rep.errors[0][1]
    assert _count(conn, "availability") == 0


def test_marketplace_unknown_isin_is_row_error(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    row = _mkt_row().replace("INE000T00101", "INE000T00888", 1)
    rep = ci.import_marketplace(conn, _market_file(tmp_path, [row]))
    assert rep.rows_ok == 0
    assert rep.errors == [(2, "unknown isin, import instrument master first")]
    assert _count(conn, "instrument") == 1


def test_marketplace_reimport_is_idempotent(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    p = _market_file(tmp_path, [_mkt_row()])
    first = ci.import_marketplace(conn, p)
    second = ci.import_marketplace(conn, p)
    assert first.rows_ok == 1
    assert second.rows_ok == 0 and second.duplicates == 1 and second.errors == []
    assert _count(conn, "price_snapshot") == 1
    assert _count(conn, "availability") == 1


def test_marketplace_new_observation_appends_snapshot(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row()], name="a.csv"))
    ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row(price="986.00", obs="2026-10-30")], name="b.csv"))
    assert _count(conn, "price_snapshot") == 2
    assert _count(conn, "availability") == 2


def test_marketplace_observed_at_required_and_iso(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    rep = ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row(obs="31-10-2026")]))
    assert rep.rows_ok == 0 and "observed_at" in rep.errors[0][1]


def test_marketplace_bad_number_is_row_error(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    rep = ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row(price="abc")]))
    assert rep.rows_ok == 0 and "price" in rep.errors[0][1]
    assert _count(conn, "price_snapshot") == 0


def test_marketplace_blank_price_and_ytm_writes_only_availability(tmp_path, conn):
    _instrument_row(tmp_path, conn)
    rep = ci.import_marketplace(conn, _market_file(tmp_path, [_mkt_row(price="", ytm="")]))
    assert rep.rows_ok == 1
    assert _count(conn, "price_snapshot") == 0
    assert _count(conn, "availability") == 1


def test_master_source_url_and_retrieved_on_persisted(conn):
    ci.import_instrument_master(conn, MASTER_FIX)
    row = conn.execute("SELECT source_url, retrieved_on, coupon_hint FROM instrument WHERE isin='INE000T00001'").fetchone()
    assert row["source_url"] == "SYNTHETIC TEST DATA"
    assert row["retrieved_on"] == "2026-10-31"
    assert row["coupon_hint"] is None


def test_blank_source_columns_become_null(tmp_path, conn):
    cells = [""] * len(ci.MASTER_HEADER)
    cells[0] = "INE000T00110"
    cells[ci.MASTER_HEADER.index("retrieved_on")] = ""
    rep = ci.import_instrument_master(conn, _master_file(tmp_path, [",".join(cells)]))
    assert rep.rows_ok == 1
    row = conn.execute("SELECT source_url, retrieved_on FROM instrument WHERE isin='INE000T00110'").fetchone()
    assert row["source_url"] is None and row["retrieved_on"] is None


def test_ratings_source_type_and_ref_persisted(conn):
    ci.import_instrument_master(conn, MASTER_FIX)
    ci.import_ratings(conn, RATINGS_FIX)
    row = conn.execute("SELECT source_type, source_ref FROM rating WHERE agency='SYNTHETIC AGENCY X' AND rating='AA+'").fetchone()
    assert (row["source_type"], row["source_ref"]) == ("manual", "SYNTHETIC TEST DATA")


def test_marketplace_source_url_notes_persisted(conn):
    ci.import_instrument_master(conn, MASTER_FIX)
    ci.import_marketplace(conn, MARKET_FIX)
    snap = conn.execute("SELECT source_url, notes FROM price_snapshot WHERE isin='INE000T00001'").fetchone()
    avail = conn.execute("SELECT source_url FROM availability WHERE isin='INE000T00001'").fetchone()
    assert (snap["source_url"], snap["notes"]) == ("SYNTHETIC TEST DATA", "SYNTHETIC TEST DATA")
    assert avail["source_url"] == "SYNTHETIC TEST DATA"


def test_marketplace_blank_notes_and_source_url_are_null(tmp_path, conn):
    ci.import_instrument_master(conn, MASTER_FIX)
    row = "INE000T00101,SYNTHETIC PLATFORM P,,,1000,985.50,9.80,,,,10000,open,,,2026-10-31,,"
    ci.import_instrument_master(conn, _master_file(tmp_path, [MASTER_LINE]))
    rep = ci.import_marketplace(conn, _market_file(tmp_path, [row]))
    assert rep.rows_ok == 1, rep.errors
    snap = conn.execute("SELECT source_url, notes FROM price_snapshot").fetchone()
    avail = conn.execute("SELECT source_url FROM availability").fetchone()
    assert snap["source_url"] is None and snap["notes"] is None
    assert avail["source_url"] is None


def test_marketplace_observed_at_accepts_iso_datetime(tmp_path):
    import shutil
    from india_bonds.database import db
    from india_bonds.data_sources import csv_import
    src = tmp_path / "m.csv"
    lines = open("tests/fixtures/sample_marketplace.csv").read().splitlines()
    hdr = lines[0].split(",")
    idx = hdr.index("observed_at")
    row = lines[1].split(",")
    row[idx] = "2026-10-29T10:00:00Z"
    src.write_text("\n".join([lines[0], ",".join(row)]) + "\n")
    c = db.connect(tmp_path / "x.sqlite"); db.init_schema(c)
    db.upsert_instrument(c, row[0], name="SYNTHETIC", exchange_listed=1)
    rep = csv_import.import_marketplace(c, src)
    assert rep.errors == [] and rep.rows_ok == 1


def test_source_label_never_contains_absolute_home_path(tmp_path):
    from india_bonds.data_sources.csv_import import source_label
    f = tmp_path / "ratings.csv"; f.write_text("x")
    assert source_label(f) == "ratings.csv"            # outside project -> basename only
    assert source_label("tests/fixtures/sample_ratings.csv") == "tests/fixtures/sample_ratings.csv"
