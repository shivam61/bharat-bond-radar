"""Tests for india_bonds.dashboard.queries (no streamlit).

All ISINs, issuers, ratings and prices here are SYNTHETIC test data.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from india_bonds.dashboard import queries as q
from india_bonds.database import db
from india_bonds.screening.screener import run_screen

SETTLE = date(2026, 10, 30)
SOURCE = "SYNTHETIC TEST DATA"

PRIME_HI = "INE000Q00001"   # AAA, annual 9%, marketplace latest (vendor_ytm 11.1), exec verified
PRIME_MON = "INE000Q00002"  # AA+, monthly 10%, discounted to 900, not executable, offered
SPEC_Q = "INE000Q00003"     # A+, quarterly 11%, exec verified, not offered
SPEC_A = "INE000Q00004"     # A, annual 12%, no availability row at all
REJ_MAT = "INE000Q00005"    # matured
REJ_BBB = "INE000Q00006"    # BBB
REJ_NOP = "INE000Q00007"    # no price

BOND = dict(
    name="SYNTHETIC BOND",
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


def _bond(conn, isin, issuer, **over):
    fields = dict(BOND, issuer_id=db.upsert_issuer(conn, issuer))
    fields.update(over)
    db.upsert_instrument(conn, isin, **fields)


def _rating(conn, isin, rating, agency="CRISIL"):
    db.insert_rating(
        conn, isin=isin, agency=agency, rating=rating, outlook=None, action="assigned",
        action_date="2026-01-15", rationale_url=None, source_id=None,
        source_type="manual", source_ref=SOURCE,
    )


def _price(conn, isin, clean, trade_date="2026-10-29", kind="exchange_close", platform=None,
           vendor_ytm=None, observed_at=None):
    db.insert_price_snapshot(
        conn, isin=isin, trade_date=trade_date, clean_price=clean, volume=None, value=None,
        trades=None, kind=kind, platform=platform, vendor_ytm=vendor_ytm, freshness="delayed",
        source_id=None, observed_at=observed_at, source_url=SOURCE,
    )


def _avail(conn, isin, *, executable, offered, observed="2026-10-29T10:00:00Z", min_inv=None):
    db.insert_availability(
        conn, isin=isin, listed=1, recently_traded=1, offered_for_sale=offered,
        price_verified=1, executable_verified=executable, min_investment=min_inv,
        platform=None, observed_at=observed, source_id=None, source_url=SOURCE,
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "dash.sqlite")
    db.init_schema(c)
    _seed(c)
    run_screen(c, SETTLE, run_id="run-new")
    yield c
    c.close()


def _seed(c):
    _bond(c, PRIME_HI, "SYNTHETIC ISSUER A")
    _rating(c, PRIME_HI, "AAA")
    _price(c, PRIME_HI, 980.0)
    _price(c, PRIME_HI, 985.0, trade_date=None, kind="marketplace", platform="SYNTHETIC PLATFORM",
           vendor_ytm=11.1, observed_at="2026-10-30T09:00:00Z")
    _avail(c, PRIME_HI, executable=1, offered=0, min_inv=10000.0)

    _bond(c, PRIME_MON, "SYNTHETIC ISSUER B", coupon_pct=10.0, coupon_frequency="monthly",
          maturity_date="2029-10-30")
    _rating(c, PRIME_MON, "AA+")
    _price(c, PRIME_MON, 900.0)
    _avail(c, PRIME_MON, executable=0, offered=1)

    _bond(c, SPEC_Q, "SYNTHETIC ISSUER C", coupon_pct=11.0, coupon_frequency="quarterly",
          maturity_date="2028-10-30")
    _rating(c, SPEC_Q, "A+")
    _price(c, SPEC_Q, 950.0)
    _avail(c, SPEC_Q, executable=1, offered=0)

    _bond(c, SPEC_A, "SYNTHETIC ISSUER D", coupon_pct=12.0, maturity_date="2031-10-30")
    _rating(c, SPEC_A, "A")
    _price(c, SPEC_A, 1000.0)

    _bond(c, REJ_MAT, "SYNTHETIC ISSUER E", maturity_date="2026-10-01")
    _rating(c, REJ_MAT, "AA+")
    _price(c, REJ_MAT, 990.0)

    _bond(c, REJ_BBB, "SYNTHETIC ISSUER F")
    _rating(c, REJ_BBB, "BBB")
    _price(c, REJ_BBB, 990.0)

    _bond(c, REJ_NOP, "SYNTHETIC ISSUER G")
    _rating(c, REJ_NOP, "AA+")


def test_latest_run_by_created_at(conn):
    run_screen(conn, SETTLE, run_id="run-old")
    conn.execute("UPDATE screen_result SET created_at = '2020-01-01T00:00:00+00:00' WHERE run_id = 'run-old'")
    conn.commit()
    run = q.latest_run(conn)
    assert run["run_id"] == "run-new"


def test_latest_run_parses_settlement_from_default_run_id(conn):
    run_screen(conn, SETTLE)  # default run_id 'screen-2026-10-30'
    run = q.latest_run(conn)
    assert run["run_id"] == "screen-2026-10-30"
    assert run["settlement"] == "2026-10-30"
    assert q.settlement_from_run_id("run-new") is None


def test_primary_and_speculative_are_separate(conn):
    prim = q.shortlist(conn, "run-new", "primary")
    spec = q.shortlist(conn, "run-new", "speculative")
    assert set(prim["isin"]) == {PRIME_HI, PRIME_MON}
    assert set(spec["isin"]) == {SPEC_Q, SPEC_A}
    assert set(prim["bucket"]) == {"primary"}
    assert set(spec["bucket"]) == {"speculative"}
    assert not set(prim["isin"]) & set(spec["isin"])


def test_rejected_isins_not_in_either_shortlist(conn):
    both = set(q.shortlist(conn, "run-new", "primary")["isin"]) | set(
        q.shortlist(conn, "run-new", "speculative")["isin"])
    assert not both & {REJ_MAT, REJ_BBB, REJ_NOP}


def test_vendor_ytm_is_separate_and_not_our_ytm(conn):
    prim = q.shortlist(conn, "run-new", "primary").set_index("isin")
    row = prim.loc[PRIME_HI]
    assert row["vendor_ytm"] == pytest.approx(11.1)
    assert row["ytm_pct"] == pytest.approx(
        conn.execute("SELECT ytm FROM screen_result WHERE run_id='run-new' AND isin=?",
                     (PRIME_HI,)).fetchone()["ytm"] * 100.0)
    assert row["ytm_pct"] != pytest.approx(11.1)
    assert "vendor_ytm" in q.SHORTLIST_COLUMNS and "ytm_pct" in q.SHORTLIST_COLUMNS
    disp = q.display_frame(prim.reset_index())
    assert "Vendor YTM (unverified)" in disp.columns and "Our YTM (%)" in disp.columns


def test_availability_flags_are_separate_columns(conn):
    prim = q.shortlist(conn, "run-new", "primary").set_index("isin")
    assert prim.loc[PRIME_HI, "executable_verified"] == 1
    assert prim.loc[PRIME_HI, "offered_for_sale"] == 0
    assert prim.loc[PRIME_MON, "executable_verified"] == 0
    assert prim.loc[PRIME_MON, "offered_for_sale"] == 1
    disp = q.display_frame(prim.reset_index())
    assert {"Executable verified", "Offered for sale"} <= set(disp.columns)
    assert set(disp.loc[disp["ISIN"] == PRIME_MON, "Offered for sale"]) == {"Yes"}
    assert set(disp.loc[disp["ISIN"] == PRIME_MON, "Executable verified"]) == {"No"}


def test_latest_availability_row_wins(conn):
    _avail(conn, SPEC_Q, executable=0, offered=1, observed="2026-10-31T10:00:00Z", min_inv=25000.0)
    _avail(conn, SPEC_Q, executable=1, offered=0, observed="2026-10-01T10:00:00Z", min_inv=1.0)
    spec = q.shortlist(conn, "run-new", "speculative").set_index("isin")
    assert spec.loc[SPEC_Q, "min_investment"] == pytest.approx(25000.0)
    assert spec.loc[SPEC_Q, "executable_verified"] == 0


def test_ranking_by_ytm_desc_within_frame(conn):
    prim = q.shortlist(conn, "run-new", "primary")
    ranked = q.add_rank(prim)
    assert list(ranked["rank"]) == list(range(1, len(ranked) + 1))
    assert ranked["ytm_pct"].is_monotonic_decreasing
    # re-ranks within a filtered frame
    only = q.add_rank(q.apply_filters(prim, ratings=["AA+"]))
    assert list(only["rank"]) == [1]
    assert only.iloc[0]["isin"] == PRIME_MON


def test_discount_flags_and_discounted_tab(conn):
    disc = q.discounted(conn, "run-new")
    # PRIME_MON 900/1000 -> 10% discount; SPEC_Q 950/1000 -> 5% discount; others <= 2%
    assert set(disc["isin"]) == {PRIME_MON, SPEC_Q}
    flags = {r["isin"]: r for _, r in disc.iterrows()}
    assert bool(flags[PRIME_MON]["below_98"]) and bool(flags[PRIME_MON]["below_95"])
    assert not bool(flags[PRIME_MON]["below_90"])
    assert bool(flags[SPEC_Q]["below_98"]) and not bool(flags[SPEC_Q]["below_95"])  # 5.0 is not > 5
    shown = q.display_frame(disc, bucket=True, discount_flags=True).set_index("ISIN")
    assert shown.loc[PRIME_MON, "Bucket"] == "Primary"
    assert shown.loc[PRIME_MON, "Below 90"] == "No"
    assert shown.loc[SPEC_Q, "Bucket"] == "Speculative (A+/A)"
    assert shown.loc[SPEC_Q, "Below 95"] == "No"


def test_discount_threshold_boundaries_match_analytics(conn):
    # 10% exactly is not below_90 (strict '>'), mirrors analytics.ytm.
    df = q.shortlist(conn, "run-new", "primary")
    df.loc[:, "discount_pct"] = 10.0
    out = q._with_derived(df)
    assert out["below_98"].all() and out["below_95"].all() and not out["below_90"].any()


def test_income_tab_mixes_buckets_and_marks_them(conn):
    inc = q.income(conn, "run-new")
    assert set(inc["isin"]) == {PRIME_MON, SPEC_Q}
    assert set(inc.loc[inc["isin"] == PRIME_MON, "bucket"]) == {"primary"}
    assert set(inc.loc[inc["isin"] == SPEC_Q, "bucket"]) == {"speculative"}
    assert set(inc["coupon_frequency"]) <= set(q.INCOME_FREQUENCIES)
    labels = q.display_frame(inc, bucket=True)["Bucket"].tolist()
    assert set(labels) == {"Primary", "Speculative (A+/A)"}


def test_rejected_grouped_per_isin_with_reasons(conn):
    rej = q.rejected(conn, "run-new").set_index("isin")
    assert set(rej.index) == {REJ_MAT, REJ_BBB, REJ_NOP}
    assert "matured" in rej.loc[REJ_MAT, "rules"]
    assert "2026-10-01" in rej.loc[REJ_MAT, "reasons"]
    assert "BBB" in rej.loc[REJ_BBB, "reasons"]


def test_rejection_counts_per_rule(conn):
    counts = q.rejection_counts(conn, "run-new").set_index("rule")["count"].to_dict()
    assert counts["matured"] == 1
    assert counts["rating_below_A"] == 1
    assert counts["stale_price"] == 1
    assert sum(counts.values()) == 3


def test_filter_ratings(conn):
    prim = q.shortlist(conn, "run-new", "primary")
    assert set(q.apply_filters(prim, ratings=["AAA"])["isin"]) == {PRIME_HI}
    assert len(q.apply_filters(prim)) == 2


def test_filter_ytm_range(conn):
    prim = q.shortlist(conn, "run-new", "primary")
    lo = prim["ytm_pct"].min()
    hi = prim["ytm_pct"].max()
    assert len(q.apply_filters(prim, ytm_range=(lo, hi))) == 2
    assert q.apply_filters(prim, ytm_range=(hi + 1, hi + 2)).empty


def test_filter_maturity_before(conn):
    prim = q.shortlist(conn, "run-new", "primary")
    out = q.apply_filters(prim, maturity_before=date(2030, 1, 1))
    assert set(out["isin"]) == {PRIME_MON}


def test_filter_frequency_and_issuer_search(conn):
    income = q.income(conn, "run-new")
    assert set(q.apply_filters(income, frequencies=["monthly"])["isin"]) == {PRIME_MON}
    prim = q.shortlist(conn, "run-new", "primary")
    assert set(q.apply_filters(prim, issuer_query="issuer a")["isin"]) == {PRIME_HI}
    assert q.apply_filters(prim, issuer_query="no such issuer").empty


def test_filter_min_confidence_and_low_marker(conn):
    spec = q.shortlist(conn, "run-new", "speculative")
    high = q.apply_filters(spec, min_confidence="High")
    assert set(high["confidence"]) <= {"High"}
    assert set(high["isin"]) <= set(spec["isin"])
    marked = q._with_derived(spec)
    for _, r in marked.iterrows():
        assert (r["low_confidence"] == q.LOW_CONFIDENCE_MARKER) == (r["confidence"] == "Low")


def test_filter_discount_min(conn):
    prim = q.shortlist(conn, "run-new", "primary")
    assert set(q.apply_filters(prim, discount_min=5.0)["isin"]) == {PRIME_MON}
    assert len(q.apply_filters(prim, discount_min=50.0)) == 0


def test_filters_on_empty_frame_do_not_raise(conn):
    empty = q.shortlist(conn, "no-such-run", "primary")
    assert empty.empty
    assert q.apply_filters(empty, ratings=["AAA"], min_confidence="High").empty
    assert q.ytm_bounds(empty) is None


def test_display_frame_has_required_labels(conn):
    disp = q.display_frame(q.shortlist(conn, "run-new", "primary"))
    required = {
        "Rank", "Issuer", "ISIN", "Name", "Lowest instrument rating", "Coupon (%)", "Our YTM (%)",
        "Vendor YTM (unverified)", "Clean price", "Dirty price", "Discount (%)", "Maturity date",
        "Payout frequency", "Next interest date", "Security type", "Flags", "Confidence",
        "Low confidence", "Price date", "Price kind", "Min investment", "Executable verified",
        "Offered for sale",
    }
    assert required <= set(disp.columns)


def test_isin_detail_contents(conn):
    d = q.isin_detail(conn, PRIME_HI, "run-new")
    assert d["master"]["issuer_name"] == "SYNTHETIC ISSUER A"
    assert list(d["ratings"]["rating"]) == ["AAA"]
    assert len(d["prices"]) == 2
    assert not d["availability"].empty
    assert d["rejections"].empty
    rej = q.isin_detail(conn, REJ_BBB, "run-new")["rejections"]
    assert "rating_below_A" in set(rej["rule"])
    assert SOURCE in set(d["sources"]["url"])


def test_all_isins_for_run(conn):
    assert len(q.all_isins(conn, "run-new")) == 7


# ---------------------------------------------------------------- missing DB / tables

def test_resolve_db_path_env_and_default():
    assert str(q.resolve_db_path({"INDIA_BONDS_DB": "/x/y.db"})) == "/x/y.db"
    assert str(q.resolve_db_path({})) == q.DEFAULT_DB_PATH


def test_open_readonly_missing_file_returns_none_and_creates_nothing(tmp_path):
    p = tmp_path / "absent.sqlite"
    assert q.open_readonly(p) is None
    assert not p.exists()


def test_open_readonly_is_read_only(tmp_path):
    p = tmp_path / "ro.sqlite"
    c = db.connect(p)
    db.init_schema(c)
    c.close()
    ro = q.open_readonly(p)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO issuer (name) VALUES ('x')")
    ro.close()


def test_missing_tables_degrade_gracefully(tmp_path):
    p = tmp_path / "partial.sqlite"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE unrelated (x INTEGER)")
    c.commit()
    c.close()
    ro = q.open_readonly(p)
    assert q.latest_run(ro) is None
    assert q.shortlist(ro, "r", "primary").empty
    assert q.income(ro, "r").empty
    assert q.discounted(ro, "r").empty
    assert q.rejected(ro, "r").empty
    assert q.rejection_counts(ro, "r").empty
    assert q.all_isins(ro, "r") == []
    d = q.isin_detail(ro, "INE000Q00001", "r")
    assert d["master"] is None and d["ratings"].empty and d["sources"].empty
    ro.close()


def test_screen_result_without_instrument_tables_is_empty(tmp_path):
    p = tmp_path / "sr_only.sqlite"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE screen_result (run_id TEXT, isin TEXT, bucket TEXT, ytm REAL, "
              "created_at TEXT)")
    c.execute("INSERT INTO screen_result VALUES ('r1', 'INE000Q00001', 'primary', 0.1, "
              "'2026-01-01T00:00:00+00:00')")
    c.commit()
    c.close()
    ro = q.open_readonly(p)
    assert q.latest_run(ro)["run_id"] == "r1"
    assert q.shortlist(ro, "r1", "primary").empty  # instrument/issuer/availability missing
    ro.close()


def test_empty_database_has_no_run(tmp_path):
    p = tmp_path / "empty.sqlite"
    c = db.connect(p)
    db.init_schema(c)
    c.close()
    ro = q.open_readonly(p)
    assert q.latest_run(ro) is None
    ro.close()
