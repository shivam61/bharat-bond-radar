import sqlite3

import pytest

from india_bonds.database import db

NOW = "2026-11-01T00:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()


def _source(conn, status="ok"):
    return db.insert_source_record(
        conn, source="test", url="file:///x", fetched_at=NOW, content_type="text/csv",
        size_bytes=10, sha256="ab" * 32, raw_path="/x", status=status, note=None,
    )


def _instrument(conn, isin="INE000T00009", **kw):
    db.upsert_instrument(conn, isin, **kw)


def test_connect_sets_row_factory_and_foreign_keys(tmp_path):
    c = db.connect(tmp_path / "a.db")
    try:
        assert c.row_factory is sqlite3.Row
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        c.close()


def test_init_schema_creates_all_tables_and_is_idempotent(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= {
        "source_record", "issuer", "instrument", "rating", "price_snapshot",
        "availability", "rejection", "screen_result",
    }
    db.init_schema(conn)  # second run must not fail


def test_foreign_key_enforced_for_rating_without_instrument(conn):
    src = _source(conn)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_rating(
            conn, isin="INE000T00404", agency="A", rating="AA", outlook=None, action="assigned",
            action_date="2025-01-01", rationale_url=None, source_id=src,
        )


def test_check_constraint_rejects_bad_price_kind(conn):
    _instrument(conn)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_price_snapshot(
            conn, isin="INE000T00009", trade_date=None, clean_price=1.0, volume=None,
            value=None, trades=None, kind="scraped", platform=None, vendor_ytm=None,
            freshness=None, source_id=None, observed_at=None,
        )


def test_check_constraint_rejects_bad_source_status(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _source(conn, status="maybe")


def test_upsert_instrument_inserts_then_updates_only_given_fields(conn):
    _instrument(conn, name="first", coupon_pct=9.0)
    _instrument(conn, name="second")  # coupon_pct not passed -> preserved
    row = conn.execute("SELECT * FROM instrument WHERE isin='INE000T00009'").fetchone()
    assert row["name"] == "second"
    assert row["coupon_pct"] == 9.0
    assert conn.execute("SELECT COUNT(*) FROM instrument").fetchone()[0] == 1


def test_upsert_instrument_rejects_unknown_column(conn):
    with pytest.raises(ValueError):
        db.upsert_instrument(conn, "INE000T00009", bogus=1)


def test_upsert_issuer_is_idempotent(conn):
    a = db.upsert_issuer(conn, "SYNTHETIC ISSUER Z", sector="s")
    b = db.upsert_issuer(conn, "SYNTHETIC ISSUER Z")
    assert a == b
    assert conn.execute("SELECT sector FROM issuer WHERE id=?", (a,)).fetchone()[0] == "s"


def test_insert_rating_returns_false_on_duplicate_key(conn):
    _instrument(conn)
    src = _source(conn)
    kwargs = dict(isin="INE000T00009", agency="SYNTHETIC AGENCY X", rating="AA",
                  outlook=None, action="assigned", action_date="2025-01-01",
                  rationale_url=None, source_id=src)
    assert db.insert_rating(conn, **kwargs) is True
    assert db.insert_rating(conn, **kwargs) is False
    assert conn.execute("SELECT COUNT(*) FROM rating").fetchone()[0] == 1


def test_insert_source_record_roundtrip(conn):
    sid = _source(conn)
    row = conn.execute("SELECT * FROM source_record WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "ok"
    assert row["sha256"] == "ab" * 32


def test_instrument_source_url_and_retrieved_on_round_trip(conn):
    db.upsert_instrument(conn, "INE000T00009", source_url="SYNTHETIC TEST DATA", retrieved_on="2026-10-31")
    row = conn.execute("SELECT source_url, retrieved_on, coupon_hint FROM instrument").fetchone()
    assert row["source_url"] == "SYNTHETIC TEST DATA"
    assert row["retrieved_on"] == "2026-10-31"
    assert row["coupon_hint"] is None


def test_rating_source_type_and_ref_round_trip(conn):
    _instrument(conn)
    src = _source(conn)
    db.insert_rating(conn, isin="INE000T00009", agency="SYNTHETIC AGENCY X", rating="AA",
                     outlook=None, action="assigned", action_date="2025-01-01",
                     rationale_url=None, source_id=src, source_type="manual",
                     source_ref="SYNTHETIC TEST DATA")
    row = conn.execute("SELECT source_type, source_ref FROM rating").fetchone()
    assert (row["source_type"], row["source_ref"]) == ("manual", "SYNTHETIC TEST DATA")


def test_rating_source_type_check_rejects_unknown(conn):
    _instrument(conn)
    src = _source(conn)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_rating(conn, isin="INE000T00009", agency="A", rating="AA", outlook=None,
                         action=None, action_date="2025-01-01", rationale_url=None,
                         source_id=src, source_type="scraped")


def test_price_snapshot_and_availability_source_url_notes_round_trip(conn):
    _instrument(conn)
    src = _source(conn)
    db.insert_price_snapshot(
        conn, isin="INE000T00009", trade_date=None, clean_price=985.5, volume=None, value=None,
        trades=None, kind="marketplace", platform="P", vendor_ytm=9.8, freshness="indicative",
        source_id=src, observed_at="2026-10-31", source_url="SYNTHETIC TEST DATA", notes="SYNTHETIC TEST DATA",
    )
    db.insert_availability(
        conn, isin="INE000T00009", listed=None, recently_traded=None, offered_for_sale=1,
        price_verified=0, executable_verified=0, min_investment=None, platform="P",
        observed_at="2026-10-31", source_id=src, source_url="SYNTHETIC TEST DATA",
    )
    p = conn.execute("SELECT source_url, notes FROM price_snapshot").fetchone()
    a = conn.execute("SELECT source_url FROM availability").fetchone()
    assert (p["source_url"], p["notes"]) == ("SYNTHETIC TEST DATA", "SYNTHETIC TEST DATA")
    assert a["source_url"] == "SYNTHETIC TEST DATA"
