"""Descriptor hints in the DB: upsert semantics, refresh step, CLI flag. No network.

All ISINs and names are SYNTHETIC; descriptor strings follow the NSE naming pattern only."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

from india_bonds.database import db
from india_bonds.enrichment.descriptor import parse_descriptor
from india_bonds.refresh import format_summary, run_refresh

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh.py"
ISIN_A = "INE000A00001"
ISIN_B = "INE000A00002"
ISIN_ISSUER_ONLY = "INE000A00003"
NAME_A = "REC LIMITED SR 261 8.3 BD 31OT31 FVRS1LAC"
NAME_A_CHANGED = "REC LIMITED SR 261 8.4 BD 31OT31 FVRS1LAC"
NAME_B = "ALPHA CAPITAL SR P1 9.5 NCD 31DC35 PERP FVRS1LAC"
NAME_ISSUER_ONLY = "SYNTHETIC ISSUER LIMITED"
T1 = "2026-11-01T10:00:00+00:00"
T2 = "2026-11-02T10:00:00+00:00"


def _conn(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


def _seed(conn, rows):
    for isin, name in rows:
        db.upsert_instrument(conn, isin, name=name, instrument_type="DEBT", exchange_listed=1)
    conn.commit()


def _hint_row(conn, isin):
    return conn.execute("SELECT * FROM descriptor_hint WHERE isin = ?", (isin,)).fetchone()


# ---------- upsert_descriptor_hint

def test_upsert_new_then_unchanged_writes_nothing(tmp_path):
    conn = _conn(tmp_path / "t.db")
    _seed(conn, [(ISIN_A, NAME_A)])
    d = parse_descriptor(NAME_A)
    assert db.upsert_descriptor_hint(conn, ISIN_A, d, None, T1) == "new"
    conn.commit()
    assert db.upsert_descriptor_hint(conn, ISIN_A, d, None, T2) == "unchanged"
    row = _hint_row(conn, ISIN_A)
    assert row["observed_at"] == T1          # unchanged: first-seen time kept
    assert row["coupon_pct"] == 8.3
    assert row["maturity_date"] == "2031-10-31"
    assert row["face_value"] == 100000.0
    assert row["kind"] == "BD"
    assert row["issuer"] == "REC LIMITED"
    conn.close()


def test_upsert_changed_coupon_updates_row_and_observed_at(tmp_path):
    conn = _conn(tmp_path / "t.db")
    _seed(conn, [(ISIN_A, NAME_A)])
    db.upsert_descriptor_hint(conn, ISIN_A, parse_descriptor(NAME_A), None, T1)
    conn.commit()
    status = db.upsert_descriptor_hint(conn, ISIN_A, parse_descriptor(NAME_A_CHANGED), None, T2)
    conn.commit()
    assert status == "changed"
    row = _hint_row(conn, ISIN_A)
    assert row["coupon_pct"] == 8.4
    assert row["raw"] == NAME_A_CHANGED
    assert row["observed_at"] == T2
    assert conn.execute("SELECT COUNT(*) FROM descriptor_hint").fetchone()[0] == 1
    conn.close()


def test_upsert_stores_flags_warnings_and_null_dates(tmp_path):
    conn = _conn(tmp_path / "t.db")
    _seed(conn, [(ISIN_B, NAME_B), (ISIN_ISSUER_ONLY, NAME_ISSUER_ONLY)])
    db.upsert_descriptor_hint(conn, ISIN_B, parse_descriptor(NAME_B), None, T1)
    db.upsert_descriptor_hint(conn, ISIN_ISSUER_ONLY, parse_descriptor(NAME_ISSUER_ONLY), None, T1)
    conn.commit()
    b = _hint_row(conn, ISIN_B)
    assert (b["perpetual_hint"], b["at1_hint"], b["call_hint"]) == (1, 0, 0)
    assert b["maturity_date"] == "2035-12-31"
    o = _hint_row(conn, ISIN_ISSUER_ONLY)
    assert o["coupon_pct"] is None and o["maturity_date"] is None and o["face_value"] is None
    assert o["warnings"]  # parser warnings are kept as text
    conn.close()


def test_init_schema_creates_table_on_existing_db(tmp_path):
    path = tmp_path / "old.db"
    conn = _conn(path)
    conn.execute("DROP TABLE descriptor_hint")
    conn.commit()
    db.init_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "descriptor_hint" in names
    conn.close()


# ---------- refresh step

def test_refresh_writes_hints_once_then_second_run_writes_nothing(tmp_path):
    db_path = tmp_path / "data" / "bonds.db"
    conn = _conn(db_path)
    _seed(conn, [(ISIN_A, NAME_A), (ISIN_B, NAME_B), (ISIN_ISSUER_ONLY, NAME_ISSUER_ONLY)])
    conn.close()

    def run():
        return run_refresh(db_path, date(2026, 11, 2), True, tmp_path / "inputs",
                           tmp_path / "reports", False)

    first = run()
    assert (first.hints_new, first.hints_changed, first.hints_unchanged) == (2, 0, 0)
    assert first.hints_unparseable == 1 and first.hints_issuer_only == 1
    assert first.db_writes >= 2
    assert "descriptor hints: new=2 changed=0 unchanged=0 unparseable=1 (issuer-only names: 1)" \
        in format_summary(first)

    second = run()
    assert (second.hints_new, second.hints_changed, second.hints_unchanged) == (0, 0, 2)
    assert second.db_writes == 0
    assert "DB unchanged" in format_summary(second)


def test_refresh_hint_source_id_is_latest_ok_corp_trades_record(tmp_path):
    db_path = tmp_path / "data" / "bonds.db"
    conn = _conn(db_path)
    _seed(conn, [(ISIN_A, NAME_A)])
    ok_old = db.insert_source_record(conn, source="nse_corp_trades", url="u1", fetched_at=T1,
                                     content_type="application/json", size_bytes=1, sha256="a",
                                     raw_path="p", status="ok")
    ok_new = db.insert_source_record(conn, source="nse_corp_trades", url="u1", fetched_at=T2,
                                     content_type="application/json", size_bytes=1, sha256="b",
                                     raw_path="p", status="ok")
    db.insert_source_record(conn, source="nse_corp_trades", url="u1", fetched_at=T2,
                            content_type=None, size_bytes=None, sha256=None,
                            raw_path=None, status="error", note="x")
    conn.commit()
    conn.close()
    run_refresh(db_path, date(2026, 11, 2), True, tmp_path / "inputs", tmp_path / "reports", False)
    conn = db.connect(db_path)
    assert _hint_row(conn, ISIN_A)["source_id"] == ok_new
    assert ok_old != ok_new
    conn.close()


def test_refresh_with_no_descriptor_names_reports_zeros(tmp_path):
    db_path = tmp_path / "data" / "bonds.db"
    _conn(db_path).close()
    s = run_refresh(db_path, date(2026, 11, 2), True, tmp_path / "inputs", tmp_path / "reports", False)
    assert (s.hints_new, s.hints_changed, s.hints_unchanged, s.hints_unparseable) == (0, 0, 0, 0)
    assert "descriptor hints: new=0 changed=0 unchanged=0 unparseable=0 (issuer-only names: 0)" \
        in format_summary(s)


def test_no_hints_flag_skips_step_in_api_and_cli(tmp_path):
    db_path = tmp_path / "data" / "bonds.db"
    conn = _conn(db_path)
    _seed(conn, [(ISIN_A, NAME_A)])
    conn.close()

    s = run_refresh(db_path, date(2026, 11, 2), True, tmp_path / "inputs", tmp_path / "reports",
                    False, hints=False)
    assert s.hints_new is None
    assert "descriptor hints" not in format_summary(s)
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM descriptor_hint").fetchone()[0] == 0
    conn.close()

    base = [sys.executable, str(SCRIPT), "--db", str(db_path), "--date", "2026-11-02",
            "--offline", "--no-report", "--inputs", str(tmp_path / "inputs")]
    with_flag = subprocess.run(base + ["--no-hints"], capture_output=True, text=True)
    assert with_flag.returncode == 0, with_flag.stderr
    assert "descriptor hints" not in with_flag.stdout
    without = subprocess.run(base, capture_output=True, text=True)
    assert without.returncode == 0, without.stderr
    assert "descriptor hints: new=1" in without.stdout
