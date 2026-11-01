"""Tests for india_bonds.analytics.ytm. Expected values are hand-derived (closed form)."""

from datetime import date

import pytest

from india_bonds.analytics.cashflows import CashFlow
from india_bonds.analytics.ytm import compute_ytm, xirr_yield


def pv_act365(flows, settlement, y):
    """Independent PV: sum amount / (1+y)^(days/365)."""
    return sum(a / (1 + y) ** ((d - settlement).days / 365.0) for d, a in flows)


# --- Hand-calc anchors ------------------------------------------------------

def test_anchor1_zero_731_days():
    # face 100, price 80, settlement 2027-01-01, maturity 2029-01-01 (731 days, 2028 leap)
    res = compute_ytm(100, 0, "zero", date(2029, 1, 1), date(2027, 1, 1), 80)
    days = (date(2029, 1, 1) - date(2027, 1, 1)).days
    assert days == 731
    expected = (100 / 80) ** (365 / 731) - 1  # ~0.11787
    assert expected == pytest.approx(0.11787, abs=1e-5)
    assert res.ytm == pytest.approx(expected, abs=1e-4)
    assert res.ytm == pytest.approx(expected, rel=1e-9)
    assert res.accrued == 0.0
    assert res.dirty == 80.0


def test_anchor2_zero_730_days_is_sqrt_1_25():
    # maturity 2028-12-31 -> 730 days -> (1.25)^(365/730) - 1 = sqrt(1.25) - 1
    assert (date(2028, 12, 31) - date(2027, 1, 1)).days == 730
    res = compute_ytm(100, 0, "zero", date(2028, 12, 31), date(2027, 1, 1), 80)
    assert res.ytm == pytest.approx(0.118034, abs=1e-6)
    assert res.ytm == pytest.approx(1.25 ** 0.5 - 1, rel=1e-9)


def test_anchor3_par_annual_10pct_on_coupon_date_yields_about_10pct():
    # coupons 2027-01-01, 2028-01-01, 2029-01-01; settlement 2026-01-01 (a coupon date)
    res = compute_ytm(
        100, 10, "annual", date(2029, 1, 1), date(2026, 1, 1), 100,
        first_interest_date=date(2027, 1, 1),
    )
    assert res.accrued == 0.0
    assert res.ytm == pytest.approx(0.10, abs=1e-3)
    # independent check: plugging the yield into the explicit flows reprices to 100
    flows = [(date(2027, 1, 1), 10.0), (date(2028, 1, 1), 10.0), (date(2029, 1, 1), 110.0)]
    assert pv_act365(flows, date(2026, 1, 1), res.ytm) == pytest.approx(100.0, abs=1e-6)


def test_anchor4_quarterly_mid_period_accrued_and_dirty():
    # quarterly 9%, face 1000, clean 1000, settlement 2029-01-10, prev coupon 2028-12-15
    # 30/360 days = 25 -> accrued = 1000 * 0.09 * 25 / 360 = 6.25
    res = compute_ytm(
        1000, 9, "quarterly", date(2029, 6, 15), date(2029, 1, 10), 1000,
        first_interest_date=date(2028, 12, 15),
    )
    expected_accrued = 1000 * 0.09 * 25 / 360
    assert res.accrued == pytest.approx(expected_accrued, abs=1e-9)
    assert res.dirty == pytest.approx(1000 + expected_accrued, abs=1e-9)
    assert res.ytm is not None
    assert res.flags == []


def test_anchor5_discount_flags_940_and_880():
    kwargs = dict(face_value=1000, coupon_pct=0, frequency="zero",
                  maturity=date(2029, 1, 1), settlement=date(2027, 1, 1))
    r940 = compute_ytm(clean_price_per_face_unit=940, **kwargs)
    assert set(r940.flags) == {"below_98", "below_95"}
    assert "below_90" not in r940.flags
    assert r940.discount_pct == pytest.approx(6.0)

    r880 = compute_ytm(clean_price_per_face_unit=880, **kwargs)
    assert set(r880.flags) == {"below_98", "below_95", "below_90"}
    assert r880.discount_pct == pytest.approx(12.0)


def test_premium_flag_and_no_discount_flags_above_par():
    res = compute_ytm(
        1000, 0, "zero", date(2029, 1, 1), date(2027, 1, 1), 1010
    )
    assert res.flags == ["premium"]
    assert res.discount_pct == pytest.approx(-1.0)


def test_anchor6_monthly_yield_reprices_explicit_schedule():
    # monthly 12% on face 1000 at par, settlement on coupon date 2026-06-30
    # coupons 10.00 on each month-end through 2027-06-30 (12 coupons) + 1000 principal
    res = compute_ytm(
        1000, 12, "monthly", date(2027, 6, 30), date(2026, 6, 30), 1000,
        first_interest_date=date(2026, 7, 31),
    )
    month_ends = [
        date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 31),
        date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31), date(2027, 2, 28),
        date(2027, 3, 31), date(2027, 4, 30), date(2027, 5, 31), date(2027, 6, 30),
    ]
    flows = [(d, 10.0) for d in month_ends]
    flows[-1] = (date(2027, 6, 30), 10.0 + 1000.0)
    assert res.accrued == 0.0
    assert pv_act365(flows, date(2026, 6, 30), res.ytm) == pytest.approx(1000.0, abs=1e-6)
    # effective annual of 1% per month is 12.68%; actual-day timing shifts it slightly
    assert res.ytm == pytest.approx((1.01) ** 12 - 1, abs=2e-3)


def test_anchor7_settlement_on_or_after_maturity_gives_none():
    res = compute_ytm(1000, 9, "annual", date(2029, 1, 1), date(2029, 1, 1), 990,
                      issue_date=date(2026, 1, 1))
    assert res.ytm is None
    res = compute_ytm(1000, 9, "annual", date(2029, 1, 1), date(2030, 1, 1), 990,
                      issue_date=date(2026, 1, 1))
    assert res.ytm is None


def test_anchor7_nonpositive_price_raises():
    with pytest.raises(ValueError):
        compute_ytm(1000, 9, "zero", date(2029, 1, 1), date(2027, 1, 1), 0)
    with pytest.raises(ValueError):
        compute_ytm(1000, 9, "zero", date(2029, 1, 1), date(2027, 1, 1), -5)
    with pytest.raises(ValueError):
        xirr_yield([CashFlow(date(2029, 1, 1), 100.0, "principal")], date(2027, 1, 1), 0)


# --- xirr_yield solver ------------------------------------------------------

def test_xirr_long_zero_uses_closed_form():
    # price 1 on face 100 over 30 years: solver must handle a very high yield
    settle, mat = date(2027, 1, 1), date(2057, 1, 1)
    flows = [CashFlow(mat, 100.0, "principal")]
    days = (mat - settle).days
    expected = (100.0 / 1.0) ** (365.0 / days) - 1
    assert xirr_yield(flows, settle, 1.0) == pytest.approx(expected, rel=1e-9)


def test_xirr_txn_cost_increases_target():
    settle, mat = date(2027, 1, 1), date(2029, 1, 1)
    flows = [CashFlow(mat, 100.0, "principal")]
    base = xirr_yield(flows, settle, 80.0)
    with_cost = xirr_yield(flows, settle, 80.0, txn_cost_pct=1.0)
    days = (mat - settle).days
    assert with_cost == pytest.approx((100 / (80 * 1.01)) ** (365 / days) - 1, rel=1e-9)
    assert with_cost < base


def test_xirr_no_future_flows_returns_none():
    flows = [CashFlow(date(2026, 1, 1), 100.0, "principal")]
    assert xirr_yield(flows, date(2027, 1, 1), 80.0) is None
    assert xirr_yield([], date(2027, 1, 1), 80.0) is None


def test_xirr_no_sign_change_returns_none():
    # zero-amount flows: PV is always 0 and can never equal a positive target
    flows = [CashFlow(date(2029, 1, 1), 0.0, "coupon")]
    assert xirr_yield(flows, date(2027, 1, 1), 80.0) is None


def test_xirr_ignores_flows_on_or_before_settlement():
    settle = date(2027, 1, 1)
    flows = [
        CashFlow(settle, 500.0, "coupon"),  # already paid, excluded
        CashFlow(date(2029, 1, 1), 100.0, "principal"),
    ]
    only_future = [flows[1]]
    assert xirr_yield(flows, settle, 80.0) == pytest.approx(xirr_yield(only_future, settle, 80.0))
