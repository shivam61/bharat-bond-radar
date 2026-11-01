"""Tests for india_bonds.screening.screener.run_screen.

All ISINs, ratings and prices here are SYNTHETIC test data.
"""

from __future__ import annotations

from datetime import date

import pytest

from india_bonds.analytics.ytm import compute_ytm
from india_bonds.database import db
from india_bonds.screening.screener import run_screen

SETTLE = date(2026, 10, 30)
SOURCE = "SYNTHETIC TEST DATA"

BOND = dict(
    name="SYNTHETIC BOND 9.00% 2030",
    exchange_listed=1,
    instrument_type="NCD",
    face_value=1000.0,
    coupon_pct=9.0,
    coupon_frequency="annual",
    first_interest_date=None,
    maturity_date="2030-10-30",
    issue_date="2025-10-30",
    security_type="secured",
    seniority="senior",
    collateral_text=None,
    is_perpetual=0,
    is_at1=0,
    is_structured=0,
    default_flag=0,
    trading_suspended=0,
    currency="INR",
    source_url=SOURCE,
    retrieved_on="2026-10-31",
)

CTRL = "INE000T00010"
MATURED = "INE000T00011"
BBB = "INE000T00012"
NOPRICE = "INE000T00013"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "screen.sqlite")
    db.init_schema(c)
    _seed(c)
    yield c
    c.close()


def _bond(conn, isin, **over):
    fields = dict(BOND)
    fields.update(over)
    db.upsert_instrument(conn, isin, **fields)


def _rating(conn, isin, rating):
    db.insert_rating(
        conn, isin=isin, agency="CRISIL", rating=rating, outlook=None, action="assigned",
        action_date="2026-01-15", rationale_url=None, source_id=None,
        source_type="manual", source_ref=SOURCE,
    )


def _price(conn, isin, clean=990.0, trade_date="2026-10-29", kind="exchange_close",
           platform=None, vendor_ytm=None, observed_at=None):
    db.insert_price_snapshot(
        conn, isin=isin, trade_date=trade_date, clean_price=clean, volume=None, value=None,
        trades=None, kind=kind, platform=platform, vendor_ytm=vendor_ytm,
        freshness="delayed", source_id=None, observed_at=observed_at,
    )


def _avail(conn, isin):
    db.insert_availability(
        conn, isin=isin, listed=1, recently_traded=1, offered_for_sale=0, price_verified=1,
        executable_verified=1, min_investment=None, platform=None,
        observed_at="2026-10-29T10:00:00Z", source_id=None,
    )


def _seed(conn):
    # control: passes everything
    _bond(conn, CTRL)
    _rating(conn, CTRL, "AA+/Stable")
    _price(conn, CTRL)
    _avail(conn, CTRL)
    # matured
    _bond(conn, MATURED, maturity_date="2026-10-01")
    _rating(conn, MATURED, "AA+")
    _price(conn, MATURED)
    # BBB rating
    _bond(conn, BBB)
    _rating(conn, BBB, "BBB")
    _price(conn, BBB)
    # no price at all
    _bond(conn, NOPRICE)
    _rating(conn, NOPRICE, "AA+")


def _expected_ytm(clean):
    return compute_ytm(
        1000.0, 9.0, "annual", date(2030, 10, 30), SETTLE, clean,
        issue_date=date(2025, 10, 30),
    )


def test_summary_counts_buckets_and_rules(conn):
    s = run_screen(conn, SETTLE, run_id="run-a")
    assert s["instruments"] == 4
    assert s["buckets"] == {"primary": 1, "speculative": 0, "rejected": 3}
    assert s["rejections"] == {"matured": 1, "rating_below_A": 1, "stale_price": 1}
    assert s["ytm_computed"] == 1


def test_rejection_rows_persisted_with_reasons(conn):
    run_screen(conn, SETTLE, run_id="run-a")
    rows = conn.execute(
        "SELECT isin, rule, reason FROM rejection WHERE run_id = 'run-a' ORDER BY isin, rule"
    ).fetchall()
    got = {(r["isin"], r["rule"]): r["reason"] for r in rows}
    assert set(got) == {(MATURED, "matured"), (BBB, "rating_below_A"), (NOPRICE, "stale_price")}
    assert all(reason for reason in got.values())
    assert "BBB" in got[(BBB, "rating_below_A")]
    assert "2026-10-01" in got[(MATURED, "matured")]


def test_control_screen_result_matches_compute_ytm(conn):
    run_screen(conn, SETTLE, run_id="run-a")
    row = conn.execute(
        "SELECT * FROM screen_result WHERE run_id = 'run-a' AND isin = ?", (CTRL,)
    ).fetchone()
    expected = _expected_ytm(990.0)
    assert row["bucket"] == "primary"
    assert row["ytm"] == pytest.approx(expected.ytm, abs=1e-12)
    assert row["dirty_price"] == pytest.approx(expected.dirty, abs=1e-12)
    assert row["discount_pct"] == pytest.approx(expected.discount_pct, abs=1e-12)
    # sanity anchor: 9% annual, price 990, ~4 years left -> yield a bit above 9%
    assert 0.09 < row["ytm"] < 0.11
    assert row["next_interest_date"] == "2027-10-30"
    assert row["confidence"] == "High"


def test_rejected_rows_carry_no_ytm(conn):
    run_screen(conn, SETTLE, run_id="run-a")
    rows = conn.execute(
        "SELECT isin, bucket, ytm, dirty_price, discount_pct FROM screen_result "
        "WHERE run_id = 'run-a' AND isin != ?", (CTRL,)
    ).fetchall()
    assert len(rows) == 3
    for r in rows:
        assert r["bucket"] == "rejected"
        assert r["ytm"] is None and r["dirty_price"] is None and r["discount_pct"] is None


def test_latest_snapshot_is_used_and_future_ignored(conn):
    _price(conn, CTRL, clean=960.0, trade_date="2026-10-28")  # older than 990 -> not used
    _price(conn, CTRL, clean=900.0, trade_date="2026-11-02")  # after settlement -> ignored
    run_screen(conn, SETTLE, run_id="run-a")
    ytm = conn.execute(
        "SELECT ytm FROM screen_result WHERE run_id = 'run-a' AND isin = ?", (CTRL,)
    ).fetchone()["ytm"]
    assert ytm == pytest.approx(_expected_ytm(990.0).ytm, abs=1e-12)


def test_vendor_ytm_never_used_as_our_ytm(conn):
    # marketplace snapshot is the latest one; its vendor_ytm must not leak into our YTM
    _price(conn, CTRL, clean=985.0, trade_date=None, kind="marketplace",
           platform="SYNTHETIC PLATFORM", vendor_ytm=0.50, observed_at="2026-10-30T09:00:00Z")
    run_screen(conn, SETTLE, run_id="run-a")
    row = conn.execute(
        "SELECT ytm, dirty_price FROM screen_result WHERE run_id = 'run-a' AND isin = ?", (CTRL,)
    ).fetchone()
    assert row["ytm"] == pytest.approx(_expected_ytm(985.0).ytm, abs=1e-12)
    assert row["ytm"] != pytest.approx(0.50)
    assert row["dirty_price"] == pytest.approx(985.0)
    # vendor value is still retained for comparison in price_snapshot
    vendor = conn.execute(
        "SELECT vendor_ytm FROM price_snapshot WHERE isin = ? AND kind = 'marketplace'", (CTRL,)
    ).fetchone()["vendor_ytm"]
    assert vendor == 0.50


def test_rerun_with_same_run_id_is_idempotent(conn):
    s1 = run_screen(conn, SETTLE, run_id="run-a")
    s2 = run_screen(conn, SETTLE, run_id="run-a")
    assert s1 == s2
    n_screen = conn.execute("SELECT COUNT(*) FROM screen_result WHERE run_id = 'run-a'").fetchone()[0]
    n_rej = conn.execute("SELECT COUNT(*) FROM rejection WHERE run_id = 'run-a'").fetchone()[0]
    assert n_screen == 4
    assert n_rej == 3


def test_distinct_run_ids_coexist(conn):
    run_screen(conn, SETTLE, run_id="run-a")
    run_screen(conn, SETTLE, run_id="run-b")
    n = conn.execute("SELECT COUNT(*) FROM screen_result").fetchone()[0]
    assert n == 8


def test_default_run_id_is_settlement_based(conn):
    s = run_screen(conn, SETTLE)
    assert s["run_id"] == "screen-2026-10-30"
    run_screen(conn, SETTLE)
    n = conn.execute(
        "SELECT COUNT(*) FROM screen_result WHERE run_id = 'screen-2026-10-30'"
    ).fetchone()[0]
    assert n == 4


def test_empty_database_returns_zero_counts(tmp_path):
    c = db.connect(tmp_path / "empty.sqlite")
    db.init_schema(c)
    s = run_screen(c, SETTLE, run_id="none")
    assert s["instruments"] == 0
    assert s["buckets"] == {"primary": 0, "speculative": 0, "rejected": 0}
    assert s["rejections"] == {}
    c.close()


def test_screen_result_persists_rating_price_flags_and_vendor_ytm(conn):
    """Architect addition: shortlist row must carry rating, price provenance and conflict flags."""
    isin = "INE000T00020"
    _bond(conn, isin)
    db.insert_rating(conn, isin=isin, agency="AGENCY1", rating="AAA", outlook=None, action="assigned",
                     action_date="2026-01-15", rationale_url=None, source_id=None,
                     source_type="manual", source_ref=SOURCE)
    db.insert_rating(conn, isin=isin, agency="AGENCY2", rating="A+", outlook=None, action="assigned",
                     action_date="2026-01-16", rationale_url=None, source_id=None,
                     source_type="manual", source_ref=SOURCE)
    _price(conn, isin, clean=980.0, kind="marketplace", platform="synthetic", vendor_ytm=11.1,
           observed_at="2026-10-29T10:00:00Z")
    run_screen(conn, SETTLE, run_id="p")
    r = conn.execute("SELECT * FROM screen_result WHERE run_id='p' AND isin=?", (isin,)).fetchone()
    assert r["bucket"] == "speculative" and r["worst_rating"] == "A+"
    assert "rating_conflict" in r["flags"]
    assert r["clean_price"] == 980.0 and r["price_kind"] == "marketplace"
    assert r["vendor_ytm"] == 11.1 and r["ytm"] != 11.1 / 100 and r["ytm"] is not None


def test_eligible_bond_without_computable_ytm_is_rejected(conn, monkeypatch):
    from india_bonds.screening import screener
    monkeypatch.setattr(screener, "_compute_ytm_for", lambda *a, **k: None)
    run_screen(conn, SETTLE, run_id="q")
    r = conn.execute("SELECT bucket FROM screen_result WHERE run_id='q' AND isin=?", (CTRL,)).fetchone()
    assert r["bucket"] == "rejected"
    assert conn.execute("SELECT 1 FROM rejection WHERE run_id='q' AND isin=? AND rule='ytm_not_computable'",
                        (CTRL,)).fetchone()
