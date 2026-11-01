"""Tests for india_bonds.analytics.cashflows. Expected values are hand-derived."""

from datetime import date

import pytest

from india_bonds.analytics.cashflows import (
    CashFlow,
    accrued_interest,
    build_schedule,
    future_flows,
    next_interest_date,
)


def coupon_dates(schedule):
    return [f.date for f in schedule if f.kind == "coupon"]


# --- zero / at_maturity -----------------------------------------------------

def test_zero_coupon_single_principal_flow():
    sched = build_schedule(100, 0, "zero", date(2029, 1, 1))
    assert sched == [CashFlow(date(2029, 1, 1), 100.0, "principal")]


def test_at_maturity_single_principal_flow():
    sched = build_schedule(1000, 9.5, "at_maturity", date(2030, 6, 15))
    assert len(sched) == 1
    assert sched[0].kind == "principal"
    assert sched[0].amount == 1000.0
    assert sched[0].date == date(2030, 6, 15)


def test_zero_accrued_is_zero_and_no_next_interest():
    sched = build_schedule(100, 0, "zero", date(2029, 1, 1))
    assert accrued_interest(100, 0, "zero", date(2029, 1, 1), date(2027, 3, 1)) == 0.0
    assert next_interest_date(sched, date(2027, 3, 1)) is None


# --- schedule dates and amounts ---------------------------------------------

def test_quarterly_schedule_dates_with_eom_clamp():
    # maturity 2029-08-31 is month-end -> all coupon dates month-end
    sched = build_schedule(
        1000, 9, "quarterly", date(2029, 8, 31), first_interest_date=date(2028, 11, 30)
    )
    assert coupon_dates(sched) == [
        date(2028, 11, 30),
        date(2029, 2, 28),  # clamped (not leap)
        date(2029, 5, 31),
        date(2029, 8, 31),
    ]
    # quarterly coupon = 1000 * 9 / 100 / 4 = 22.5
    assert all(f.amount == pytest.approx(22.5) for f in sched if f.kind == "coupon")
    principal = [f for f in sched if f.kind == "principal"]
    assert len(principal) == 1 and principal[0].date == date(2029, 8, 31)


def test_semiannual_schedule_dates_leap_year():
    sched = build_schedule(
        1000, 8, "semiannual", date(2030, 2, 28), first_interest_date=date(2026, 8, 31)
    )
    assert coupon_dates(sched) == [
        date(2026, 8, 31),
        date(2027, 2, 28),
        date(2027, 8, 31),
        date(2028, 2, 29),  # leap year, month-end
        date(2028, 8, 31),
        date(2029, 2, 28),
        date(2029, 8, 31),
        date(2030, 2, 28),
    ]
    # semiannual coupon = 1000 * 8 / 100 / 2 = 40
    assert all(f.amount == pytest.approx(40.0) for f in sched if f.kind == "coupon")


def test_annual_schedule_from_issue_date():
    sched = build_schedule(100, 10, "annual", date(2029, 1, 1), issue_date=date(2026, 1, 1))
    assert coupon_dates(sched) == [date(2027, 1, 1), date(2028, 1, 1), date(2029, 1, 1)]
    assert all(f.amount == pytest.approx(10.0) for f in sched if f.kind == "coupon")


def test_monthly_schedule_length_and_real_dates():
    # 12 months to maturity -> 12 coupon flows, each face*c/100/12 on real calendar dates
    sched = build_schedule(
        1000, 12, "monthly", date(2027, 6, 30), first_interest_date=date(2026, 7, 31)
    )
    expected_dates = [
        date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 31),
        date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31), date(2027, 2, 28),
        date(2027, 3, 31), date(2027, 4, 30), date(2027, 5, 31), date(2027, 6, 30),
    ]
    assert coupon_dates(sched) == expected_dates
    assert len(coupon_dates(sched)) == 12
    coupons = [f.amount for f in sched if f.kind == "coupon"]
    assert all(c == pytest.approx(1000 * 12 / 100 / 12) for c in coupons)  # 10.0 each
    assert sum(1 for f in sched if f.kind == "principal") == 1


def test_non_month_end_maturity_clamps_day_from_maturity():
    # maturity on the 30th, quarterly: 2029-08-30 -> 2029-05-30 -> 2029-02-28 -> 2028-11-30
    sched = build_schedule(
        100, 8, "quarterly", date(2029, 8, 30), first_interest_date=date(2028, 11, 30)
    )
    assert coupon_dates(sched) == [
        date(2028, 11, 30), date(2029, 2, 28), date(2029, 5, 30), date(2029, 8, 30),
    ]


def test_first_interest_date_inconsistent_raises():
    with pytest.raises(ValueError):
        build_schedule(
            1000, 9, "quarterly", date(2029, 8, 31), first_interest_date=date(2028, 11, 29)
        )


def test_first_interest_after_maturity_raises():
    with pytest.raises(ValueError):
        build_schedule(
            1000, 9, "annual", date(2029, 1, 1), first_interest_date=date(2029, 6, 1)
        )


def test_first_interest_not_after_issue_raises():
    with pytest.raises(ValueError):
        build_schedule(
            1000, 9, "annual", date(2029, 1, 1),
            first_interest_date=date(2026, 1, 1), issue_date=date(2026, 1, 1),
        )


def test_no_first_or_issue_date_raises():
    with pytest.raises(ValueError):
        build_schedule(1000, 9, "annual", date(2029, 1, 1))


def test_unknown_frequency_raises():
    with pytest.raises(ValueError):
        build_schedule(1000, 9, "fortnightly", date(2029, 1, 1), issue_date=date(2026, 1, 1))


# --- future flows / next interest date --------------------------------------

def test_future_flows_strictly_after_settlement():
    sched = build_schedule(
        1000, 9, "quarterly", date(2029, 6, 15), first_interest_date=date(2028, 12, 15)
    )
    flows = future_flows(sched, date(2029, 3, 15))  # settlement on a coupon date
    assert all(f.date > date(2029, 3, 15) for f in flows)
    assert coupon_dates(flows) == [date(2029, 6, 15)]


def test_next_interest_date_on_and_between_coupons():
    sched = build_schedule(
        1000, 9, "quarterly", date(2029, 6, 15), first_interest_date=date(2028, 12, 15)
    )
    assert next_interest_date(sched, date(2029, 1, 10)) == date(2029, 3, 15)
    # on a coupon date, the next one is strictly after
    assert next_interest_date(sched, date(2029, 3, 15)) == date(2029, 6, 15)
    assert next_interest_date(sched, date(2029, 6, 15)) is None


# --- accrued interest --------------------------------------------------------

def test_accrued_quarterly_mid_period_30_360():
    # maturity 2029-06-15 quarterly, prev coupon 2028-12-15, settlement 2029-01-10.
    # 30/360: (2029-2028)*360 + (1-12)*30 + (10-15) = 360 - 330 - 5 = 25 days
    expected = 1000 * 0.09 * 25 / 360  # = 6.25
    got = accrued_interest(
        1000, 9, "quarterly", date(2029, 6, 15), date(2029, 1, 10),
        first_interest_date=date(2028, 12, 15),
    )
    assert got == pytest.approx(expected, abs=1e-9)
    assert got == pytest.approx(6.25, abs=1e-9)


def test_accrued_act_365_option():
    # actual days 2028-12-15 -> 2029-01-10 = 26
    expected = 1000 * 0.09 * 26 / 365
    got = accrued_interest(
        1000, 9, "quarterly", date(2029, 6, 15), date(2029, 1, 10),
        first_interest_date=date(2028, 12, 15), daycount="act/365",
    )
    assert got == pytest.approx(expected, abs=1e-9)


def test_accrued_zero_on_coupon_date():
    got = accrued_interest(
        1000, 9, "quarterly", date(2029, 6, 15), date(2029, 3, 15),
        first_interest_date=date(2028, 12, 15),
    )
    assert got == 0.0


def test_accrued_from_issue_date_before_first_coupon():
    # issue 2026-03-01, annual 10%, first coupon 2027-01-01 (non-standard stub)
    # settlement 2026-09-01: 30/360 = 360*0 + 30*(9-3) + (1-1) = 180 days
    got = accrued_interest(
        100, 10, "annual", date(2029, 1, 1), date(2026, 9, 1),
        first_interest_date=date(2027, 1, 1), issue_date=date(2026, 3, 1),
    )
    assert got == pytest.approx(100 * 0.10 * 180 / 360, abs=1e-9)


def test_accrued_zero_at_or_after_maturity():
    assert accrued_interest(
        1000, 9, "annual", date(2029, 1, 1), date(2029, 1, 1),
        issue_date=date(2026, 1, 1),
    ) == 0.0
    assert accrued_interest(
        1000, 9, "annual", date(2029, 1, 1), date(2030, 1, 1),
        issue_date=date(2026, 1, 1),
    ) == 0.0


def test_accrued_settlement_before_issue_raises():
    with pytest.raises(ValueError):
        accrued_interest(
            1000, 9, "annual", date(2029, 1, 1), date(2025, 6, 1),
            issue_date=date(2026, 1, 1),
        )


def test_accrued_unknown_daycount_raises():
    with pytest.raises(ValueError):
        accrued_interest(
            1000, 9, "annual", date(2029, 1, 1), date(2027, 6, 1),
            issue_date=date(2026, 1, 1), daycount="act/act",
        )


def test_schedule_requires_positive_face():
    with pytest.raises(ValueError):
        build_schedule(0, 9, "annual", date(2029, 1, 1), issue_date=date(2026, 1, 1))
