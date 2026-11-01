"""Tests for india_bonds.screening.eligibility.

All ISINs, ratings and prices here are SYNTHETIC test data.
"""

from __future__ import annotations

from datetime import date

import pytest

from india_bonds.database import db
from india_bonds.screening.eligibility import evaluate, latest_price, parse_rating

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
DEFAULT_RATINGS = (("CRISIL", "AA+/Stable", "assigned", "2026-01-15"),)
CTRL = "INE000T00010"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "elig.sqlite")
    db.init_schema(c)
    yield c
    c.close()


def build(conn, isin=CTRL, *, bond=None, ratings=DEFAULT_RATINGS, price=True,
          price_kw=None, avail=True, avail_exec=1):
    fields = dict(BOND)
    fields.update(bond or {})
    db.upsert_instrument(conn, isin, **fields)
    for agency, rating, action, action_date in ratings:
        db.insert_rating(
            conn, isin=isin, agency=agency, rating=rating, outlook=None, action=action,
            action_date=action_date, rationale_url=None, source_id=None,
            source_type="manual", source_ref=SOURCE,
        )
    if price:
        kw = dict(trade_date="2026-10-29", clean_price=990.0, kind="exchange_close",
                  vendor_ytm=None, observed_at=None, platform=None)
        kw.update(price_kw or {})
        db.insert_price_snapshot(
            conn, isin=isin, volume=None, value=None, trades=None, freshness="delayed",
            source_id=None, **kw,
        )
    if avail:
        db.insert_availability(
            conn, isin=isin, listed=1, recently_traded=1, offered_for_sale=0,
            price_verified=1, executable_verified=avail_exec, min_investment=None,
            platform=None, observed_at="2026-10-29T10:00:00Z", source_id=None,
        )
    return isin


def rules(result):
    return [rule for rule, _ in result.failures]


# --- control and single-rule failures -------------------------------------

def test_control_bond_is_primary_high(conn):
    build(conn)
    ev = evaluate(conn, CTRL, SETTLE, "run-1")
    assert ev.eligible is True
    assert ev.failures == []
    assert ev.bucket == "primary"
    assert ev.worst_rating == "AA+"
    assert ev.confidence == "High"
    assert ev.flags == []
    assert ev.next_interest_date == date(2027, 10, 30)


@pytest.mark.parametrize(
    "rule, bond_over",
    [
        ("no_active_isin", {"exchange_listed": 0}),
        ("no_active_isin", {"exchange_listed": None}),
        ("not_inr", {"currency": "USD"}),
        ("unverified_currency", {"currency": None}),
        ("matured", {"maturity_date": "2026-10-01"}),
        ("maturity_over_10y", {"maturity_date": "2040-10-30"}),
        ("missing_coupon_or_schedule", {"coupon_pct": None}),
        ("missing_coupon_or_schedule", {"issue_date": None, "first_interest_date": None}),
        ("missing_coupon_or_schedule", {"coupon_frequency": None}),
        ("missing_coupon_or_schedule", {"maturity_date": None}),
        ("missing_coupon_or_schedule", {"first_interest_date": "2026-03-01"}),
        ("perpetual", {"is_perpetual": 1}),
        ("at1", {"is_at1": 1}),
        ("structured", {"is_structured": 1}),
        ("default_flag", {"default_flag": 1}),
        ("trading_suspended", {"trading_suspended": 1}),
        ("unverified_is_perpetual", {"is_perpetual": None}),
        ("unverified_is_at1", {"is_at1": None}),
        ("unverified_is_structured", {"is_structured": None}),
        ("unverified_default_flag", {"default_flag": None}),
        ("unverified_trading_suspended", {"trading_suspended": None}),
    ],
)
def test_each_rule_fails_alone(conn, rule, bond_over):
    isin = build(conn, bond=bond_over)
    ev = evaluate(conn, isin, SETTLE, "run-1")
    assert ev.eligible is False
    assert ev.bucket == "rejected"
    assert rules(ev) == [rule]


def test_missing_instrument_rejected_no_active_isin(conn):
    ev = evaluate(conn, "INE000T00099", SETTLE, "run-1")
    assert rules(ev) == ["no_active_isin"]
    assert ev.bucket == "rejected"


def test_no_instrument_rating(conn):
    isin = build(conn, ratings=())
    ev = evaluate(conn, isin, SETTLE, "run-1")
    assert rules(ev) == ["no_instrument_rating"]
    assert ev.confidence == "Low"


def test_stale_price_when_older_than_7_days(conn):
    isin = build(conn, price_kw={"trade_date": "2026-10-22"})  # 8 days before settlement
    assert rules(evaluate(conn, isin, SETTLE, "r")) == ["stale_price"]


def test_price_exactly_7_days_old_is_fresh(conn):
    isin = build(conn, price_kw={"trade_date": "2026-10-23"})  # 7 days before settlement
    assert evaluate(conn, isin, SETTLE, "r").eligible is True


def test_no_price_is_stale_price(conn):
    isin = build(conn, price=False)
    ev = evaluate(conn, isin, SETTLE, "r")
    assert rules(ev) == ["stale_price"]
    assert "not price-verified" in ev.failures[0][1]


def test_future_price_is_ignored_by_evaluate(conn):
    isin = build(conn, price_kw={"trade_date": "2026-11-02", "clean_price": 900.0})
    # only a post-settlement price exists -> nothing on or before settlement
    assert rules(evaluate(conn, isin, SETTLE, "r")) == ["stale_price"]
    assert latest_price(conn, isin, SETTLE) is None


# --- rating parsing and rating rules --------------------------------------

@pytest.mark.parametrize(
    "text, canonical, flags",
    [
        ("CRISIL AA+/Stable", "AA+", ()),
        ("AA+", "AA+", ()),
        ("AA (CE)", "AA", ("credit_enhanced",)),
        ("ICRA A+(SO)", "A+", ("credit_enhanced",)),
        ("CARE AAA (CE)", "AAA", ("credit_enhanced",)),
        ("IND AA-", "AA-", ()),
        ("AA+ (Stable)", "AA+", ()),
        ("BBB–", "BBB-", ()),
        ("CARE AA+ (Watch with Negative Implications)", "AA+", ("rating_watch",)),
    ],
)
def test_parse_rating_forms(text, canonical, flags):
    p = parse_rating(text)
    assert p is not None
    assert p.canonical == canonical
    assert set(p.flags) == set(flags)


@pytest.mark.parametrize("text", [None, "", "NOT RATED", "NR", "AA+ AA", "CRISIL"])
def test_parse_rating_unparseable(text):
    assert parse_rating(text) is None


def test_unparseable_rating_rejected(conn):
    isin = build(conn, ratings=(("CRISIL", "NOT RATED", "assigned", "2026-01-15"),))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert rules(ev) == ["unparseable_rating"]
    assert ev.bucket == "rejected"


def test_withdrawn_action_is_ignored(conn):
    isin = build(conn, ratings=(
        ("CRISIL", "AA+/Stable", "assigned", "2026-01-15"),
        ("CRISIL", "BBB", "withdrawn", "2026-06-01"),
    ))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert ev.eligible is True
    assert ev.worst_rating == "AA+"
    assert "agency_withdrawn" in ev.flags


def test_most_recent_action_by_date_wins_not_insert_order(conn):
    # downgrade (later date) inserted first; older assignment inserted second
    isin = build(conn, ratings=(
        ("CRISIL", "BBB", "downgraded", "2026-03-01"),
        ("CRISIL", "AA", "assigned", "2025-01-01"),
    ))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert rules(ev) == ["rating_below_A"]
    assert ev.worst_rating == "BBB"


def test_two_agencies_conflict_lowest_wins_and_flags(conn):
    isin = build(conn, ratings=(
        ("CRISIL", "AA+", "assigned", "2026-01-15"),
        ("ICRA", "A", "assigned", "2026-01-20"),
    ))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert ev.eligible is True
    assert ev.worst_rating == "A"
    assert ev.bucket == "speculative"
    assert "rating_conflict" in ev.flags
    assert ev.confidence == "Medium"  # conflict lowers High


def test_lowest_across_agencies_one_notch_apart_is_primary(conn):
    # AAA vs AA- differ by 3 notches -> conflict flag, but lowest is AA- -> primary
    isin = build(conn, ratings=(
        ("CRISIL", "AAA", "assigned", "2026-01-15"),
        ("CARE", "AA-", "assigned", "2026-01-16"),
    ))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert ev.bucket == "primary"
    assert ev.worst_rating == "AA-"
    assert "rating_conflict" in ev.flags


def test_agreeing_agencies_no_conflict(conn):
    isin = build(conn, ratings=(
        ("CRISIL", "AA", "assigned", "2026-01-15"),
        ("ICRA", "AA", "reaffirmed", "2026-02-01"),
    ))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert ev.bucket == "primary"
    assert ev.flags == []
    assert ev.confidence == "High"


def test_a_plus_is_speculative(conn):
    isin = build(conn, ratings=(("CRISIL", "A+", "assigned", "2026-01-15"),))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert ev.eligible is True
    assert ev.bucket == "speculative"


@pytest.mark.parametrize("rating", ["A-", "BBB+", "BBB", "BB+", "D"])
def test_below_a_minus_rejected(conn, rating):
    isin = build(conn, ratings=(("CRISIL", rating, "assigned", "2026-01-15"),))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert rules(ev) == ["rating_below_A"]
    assert ev.bucket == "rejected"


def test_credit_enhanced_flag_on_instrument(conn):
    isin = build(conn, ratings=(("CRISIL", "AA (CE)", "assigned", "2026-01-15"),))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert ev.eligible is True
    assert "credit_enhanced" in ev.flags


# --- confidence ------------------------------------------------------------

def test_no_availability_is_medium(conn):
    isin = build(conn, avail=False)
    assert evaluate(conn, isin, SETTLE, "r").confidence == "Medium"


def test_not_executable_is_medium(conn):
    isin = build(conn, avail_exec=0)
    assert evaluate(conn, isin, SETTLE, "r").confidence == "Medium"


def test_no_price_and_no_availability_is_low(conn):
    isin = build(conn, price=False, avail=False)
    assert evaluate(conn, isin, SETTLE, "r").confidence == "Low"


def test_no_rating_never_high(conn):
    isin = build(conn, ratings=())
    assert evaluate(conn, isin, SETTLE, "r").confidence == "Low"


def test_conflict_caps_high_at_medium(conn):
    isin = build(conn, ratings=(
        ("CRISIL", "AA", "assigned", "2026-01-15"),
        ("ICRA", "A+", "assigned", "2026-01-20"),
    ))
    ev = evaluate(conn, isin, SETTLE, "r")
    assert "rating_conflict" in ev.flags
    assert ev.confidence == "Medium"


def test_marketplace_price_counts_as_price_verified(conn):
    isin = build(conn, price=False, avail=True)
    db.insert_price_snapshot(
        conn, isin=isin, trade_date=None, clean_price=985.0, volume=None, value=None,
        trades=None, kind="marketplace", platform="SYNTHETIC PLATFORM", vendor_ytm=10.1,
        freshness="indicative", source_id=None, observed_at="2026-10-29T12:00:00Z",
    )
    ev = evaluate(conn, isin, SETTLE, "r")
    assert ev.eligible is True
    assert ev.price is not None and ev.price.kind == "marketplace"
    assert ev.confidence == "High"


def test_later_unverified_availability_row_does_not_erase_exec_verification(tmp_path):
    """Architect fix: a routine price-ingest availability row (executable_verified=0) written after a
    manual verification must not reset exec_ok."""
    from datetime import date
    from india_bonds.database import db
    from india_bonds.screening import screener
    c = db.connect(tmp_path / "e.sqlite"); db.init_schema(c)
    import tests.test_screener as ts
    ts._seed(c)
    ts._avail(c, ts.CTRL)  # executable_verified=1 at 2026-10-29
    db.insert_availability(c, isin=ts.CTRL, listed=1, recently_traded=1, offered_for_sale=0,
                           price_verified=0, executable_verified=0, min_investment=None, platform=None,
                           observed_at="2026-10-30T09:00:00Z", source_id=None)
    from india_bonds.screening.eligibility import evaluate
    assert evaluate(c, ts.CTRL, date(2026, 10, 30)).confidence == "High"
