"""Cash-flow schedules and accrued interest for Indian corporate bonds.

Pure functions, no I/O.

Conventions:
- Coupon dates are generated BACKWARD from maturity in steps of 1/3/6/12 months.
  If maturity is a month-end, every coupon date is the month-end of its month
  (e.g. maturity 2027-06-30 quarterly -> 2027-03-31, 2026-12-31, ...).
  Otherwise relativedelta clamps the day to the month length.
- Per-period coupon = face_value * coupon_pct / 100 / periods_per_year.
- Final coupon date carries two CashFlows: kind 'coupon' and kind 'principal'.
- Accrued interest uses 30/360 (US/NASD bond basis) by default, or Act/365.
  Accrual starts at the previous coupon date (or issue_date when the first
  period is not known).

LIMITATION (Phase 1): NO business-day adjustment. Dates are the raw calendar
dates generated from maturity; weekends/holidays are not rolled to the next
business day, so yields can differ slightly from exchange-quoted figures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

_MONTHS_PER_PERIOD = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}
_SINGLE_FLOW = {"zero", "at_maturity"}
_DAYCOUNTS = {"30/360", "act/365"}


@dataclass(frozen=True)
class CashFlow:
    date: date
    amount: float
    kind: str  # 'coupon' | 'principal'


def _check_frequency(frequency: str) -> None:
    if frequency not in _MONTHS_PER_PERIOD and frequency not in _SINGLE_FLOW:
        raise ValueError(f"unknown coupon_frequency: {frequency!r}")


def _check_face_coupon(face_value: float, coupon_pct: float) -> None:
    if face_value is None or face_value <= 0:
        raise ValueError("face_value must be > 0")
    if coupon_pct is None or coupon_pct < 0:
        raise ValueError("coupon_pct must be >= 0")


def _is_month_end(d: date) -> bool:
    return (d + timedelta(days=1)).day == 1


def _grid_date(maturity: date, months: int, k: int) -> date:
    """k-th coupon date counting backward from maturity (k=0 is maturity)."""
    target = maturity + relativedelta(months=-k * months)
    if _is_month_end(maturity):
        # month-end rule: move to the last day of the target month
        return (target + relativedelta(day=31))
    return target


def _coupon_dates(
    maturity: date,
    months: int,
    first_interest_date: date | None,
    issue_date: date | None,
) -> tuple[list[date], date]:
    """Return (coupon dates ascending incl. maturity, accrual start before first coupon)."""
    if issue_date is not None and issue_date >= maturity:
        raise ValueError("issue_date must be before maturity")

    if first_interest_date is not None:
        if first_interest_date > maturity:
            raise ValueError("first_interest_date must not be after maturity")
        if issue_date is not None and first_interest_date <= issue_date:
            raise ValueError("first_interest_date must be after issue_date")
        dates: list[date] = []
        k = 0
        while True:
            d = _grid_date(maturity, months, k)
            if d < first_interest_date:
                raise ValueError(
                    "first_interest_date is inconsistent with maturity and coupon frequency"
                )
            dates.append(d)
            if d == first_interest_date:
                break
            k += 1
        dates.reverse()
        prev = issue_date if issue_date is not None else _grid_date(maturity, months, k + 1)
        return dates, prev

    if issue_date is None:
        raise ValueError("need first_interest_date or issue_date to bound the schedule")
    dates = []
    k = 0
    while True:
        d = _grid_date(maturity, months, k)
        if d <= issue_date:
            break
        dates.append(d)
        k += 1
    dates.reverse()
    return dates, issue_date


def build_schedule(
    face_value: float,
    coupon_pct: float,
    frequency: str,
    maturity: date,
    first_interest_date: date | None = None,
    issue_date: date | None = None,
) -> list[CashFlow]:
    """Build the bond's future-and-past cash-flow schedule.

    'zero' and 'at_maturity' return a single principal CashFlow on maturity;
    coupon_pct is ignored for them (no periodic coupon).
    Raises ValueError on inconsistent or insufficient dates.
    """
    _check_frequency(frequency)
    _check_face_coupon(face_value, coupon_pct)
    if frequency in _SINGLE_FLOW:
        return [CashFlow(maturity, float(face_value), "principal")]

    months = _MONTHS_PER_PERIOD[frequency]
    periods_per_year = 12 // months
    coupon = face_value * coupon_pct / 100.0 / periods_per_year
    dates, _ = _coupon_dates(maturity, months, first_interest_date, issue_date)

    flows: list[CashFlow] = []
    for d in dates:
        flows.append(CashFlow(d, coupon, "coupon"))
        if d == maturity:
            flows.append(CashFlow(d, float(face_value), "principal"))
    return flows


def future_flows(schedule: list[CashFlow], settlement: date) -> list[CashFlow]:
    """Flows strictly after settlement (a flow dated on settlement is already paid)."""
    return [f for f in schedule if f.date > settlement]


def next_interest_date(schedule: list[CashFlow], settlement: date) -> date | None:
    """Earliest coupon-kind flow strictly after settlement; None if none (e.g. zero)."""
    candidates = [f.date for f in schedule if f.kind == "coupon" and f.date > settlement]
    return min(candidates) if candidates else None


def _is_last_feb(d: date) -> bool:
    return d.month == 2 and (d + timedelta(days=1)).month == 3


def _days_30_360_us(d1: date, d2: date) -> int:
    """US (NASD) 30/360 day count, including end-of-February rules."""
    y1, m1, day1 = d1.year, d1.month, d1.day
    y2, m2, day2 = d2.year, d2.month, d2.day
    if _is_last_feb(d1) and _is_last_feb(d2):
        day2 = 30
    if _is_last_feb(d1):
        day1 = 30
    if day2 == 31 and day1 >= 30:
        day2 = 30
    if day1 == 31:
        day1 = 30
    return 360 * (y2 - y1) + 30 * (m2 - m1) + (day2 - day1)


def accrued_interest(
    face_value: float,
    coupon_pct: float,
    frequency: str,
    maturity: date,
    settlement: date,
    first_interest_date: date | None = None,
    issue_date: date | None = None,
    daycount: str = "30/360",
) -> float:
    """Accrued interest per face-value unit set (absolute amount, same units as face_value).

    Accrues from the last coupon date on/before settlement (or the accrual start
    before the first coupon). 0 for zero/at_maturity bonds, for settlement on a
    coupon date, and for settlement on/after maturity.
    """
    _check_frequency(frequency)
    _check_face_coupon(face_value, coupon_pct)
    if daycount not in _DAYCOUNTS:
        raise ValueError(f"unsupported daycount: {daycount!r}")
    if frequency in _SINGLE_FLOW or settlement >= maturity:
        return 0.0

    months = _MONTHS_PER_PERIOD[frequency]
    dates, prev_boundary = _coupon_dates(maturity, months, first_interest_date, issue_date)
    previous = None
    for c in [prev_boundary] + dates:  # ascending
        if c <= settlement:
            previous = c
    if previous is None:
        raise ValueError("settlement precedes the accrual start (issue/first period)")
    if previous == settlement:
        return 0.0

    annual_amount = face_value * coupon_pct / 100.0
    if daycount == "30/360":
        return annual_amount * _days_30_360_us(previous, settlement) / 360.0
    return annual_amount * (settlement - previous).days / 365.0
