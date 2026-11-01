"""Yield-to-maturity and discount flags. Pure functions, no I/O.

YTM is computed from the actual cash-flow schedule and the dirty price
(clean + accrued). It is never substituted with the coupon rate.

Yield convention: effective annual yield, Act/365 time (t = days / 365):
    sum(amount_i / (1+y)^t_i) = dirty_price * (1 + txn_cost_pct/100)

LIMITATION (Phase 1): no business-day adjustment of dates (see cashflows.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from india_bonds.analytics.cashflows import (
    CashFlow,
    accrued_interest,
    build_schedule,
    future_flows,
)

_LO = -0.99
_HI = 10.0


@dataclass(frozen=True)
class YtmResult:
    ytm: float | None  # effective annual, decimal (0.10 == 10%); None if not computable
    dirty: float  # clean + accrued, same units as face_value
    accrued: float
    discount_pct: float  # (face - clean) / face * 100; negative for premium
    flags: list[str]  # subset of 'below_98', 'below_95', 'below_90', 'premium'


def xirr_yield(
    flows: list[CashFlow],
    settlement: date,
    dirty_price: float,
    txn_cost_pct: float = 0.0,
) -> float | None:
    """Effective annual Act/365 yield solving PV(flows) = dirty*(1+txn/100).

    Only flows dated after settlement are used. Newton's method from a guess,
    falling back to bisection on [-0.99, 10]. Returns None when there are no
    future flows or no sign change (no root in range). Raises ValueError only
    for dirty_price <= 0.
    """
    if dirty_price is None or dirty_price <= 0:
        raise ValueError("dirty_price must be > 0")
    target = dirty_price * (1.0 + txn_cost_pct / 100.0)
    terms = [(f.amount, (f.date - settlement).days / 365.0) for f in flows if f.date > settlement]
    if not terms:
        return None

    def residual(y: float) -> float:
        log_base = math.log1p(y)
        total = 0.0
        for amount, t in terms:
            if amount == 0:
                continue
            try:
                total += amount * math.exp(-t * log_base)
            except OverflowError:
                return math.inf
        return total - target

    def slope(y: float) -> float:
        log_base = math.log1p(y)
        total = 0.0
        for amount, t in terms:
            if amount == 0:
                continue
            total += -t * amount * math.exp(-t * log_base) / (1.0 + y)
        return total

    f_lo = residual(_LO)
    f_hi = residual(_HI)
    if f_lo < 0 or f_hi > 0:  # PV is decreasing in y; need a sign change
        return None

    # Newton from a guess, abandoning it if it leaves the bracket
    y = 0.05
    try:
        for _ in range(100):
            fy = residual(y)
            dfy = slope(y)
            if dfy == 0 or not math.isfinite(fy) or not math.isfinite(dfy):
                break
            y_new = y - fy / dfy
            if not (_LO < y_new < _HI):
                break
            if abs(y_new - y) < 1e-12:
                return y_new
            y = y_new
    except (OverflowError, ZeroDivisionError):
        pass

    # Bisection fallback
    lo, hi = _LO, _HI
    for _ in range(400):
        mid = (lo + hi) / 2.0
        f_mid = residual(mid)
        if f_mid == 0 or hi - lo < 1e-13:
            return mid
        if f_mid > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _discount_flags(face_value: float, clean: float) -> tuple[float, list[str]]:
    discount_pct = (face_value - clean) / face_value * 100.0
    flags: list[str] = []
    if clean > face_value:
        flags.append("premium")
    if discount_pct > 2:
        flags.append("below_98")
    if discount_pct > 5:
        flags.append("below_95")
    if discount_pct > 10:
        flags.append("below_90")
    return discount_pct, flags


def compute_ytm(
    face_value: float,
    coupon_pct: float,
    frequency: str,
    maturity: date,
    settlement: date,
    clean_price_per_face_unit: float,
    first_interest_date: date | None = None,
    issue_date: date | None = None,
    daycount: str = "30/360",
    txn_cost_pct: float = 0.0,
) -> YtmResult:
    """YTM of a bond at a clean price quoted per face-value unit (e.g. 990 on face 1000).

    Returns ytm=None when settlement >= maturity. Raises ValueError for
    price <= 0 or face_value <= 0.
    """
    if face_value is None or face_value <= 0:
        raise ValueError("face_value must be > 0")
    if clean_price_per_face_unit is None or clean_price_per_face_unit <= 0:
        raise ValueError("clean price must be > 0")

    clean = float(clean_price_per_face_unit)
    discount_pct, flags = _discount_flags(face_value, clean)

    if settlement >= maturity:
        return YtmResult(ytm=None, dirty=clean, accrued=0.0, discount_pct=discount_pct, flags=flags)

    accrued = accrued_interest(
        face_value, coupon_pct, frequency, maturity, settlement,
        first_interest_date=first_interest_date, issue_date=issue_date, daycount=daycount,
    )
    dirty = clean + accrued
    schedule = build_schedule(
        face_value, coupon_pct, frequency, maturity,
        first_interest_date=first_interest_date, issue_date=issue_date,
    )
    ytm = xirr_yield(future_flows(schedule, settlement), settlement, dirty, txn_cost_pct)
    return YtmResult(ytm=ytm, dirty=dirty, accrued=accrued, discount_pct=discount_pct, flags=flags)
