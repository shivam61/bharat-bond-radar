"""Run the eligibility rules and YTM over every instrument and persist the results.

Idempotent per run_id: re-running with the same run_id replaces that run's
rejection and screen_result rows rather than appending duplicates.

Our YTM comes only from the latest price_snapshot's clean_price. A marketplace
vendor_ytm is never used as our YTM; it stays in price_snapshot for comparison.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from india_bonds.analytics.ytm import compute_ytm
from india_bonds.screening.eligibility import (
    SINGLE_FLOW_FREQUENCIES,
    _to_date,
    evaluate,
)


# Our YTM vs the NSE-reported traded yield (percentage points). Also a check that the NSE price
# is clean: a larger gap flags 'reported_yield_mismatch' and caps confidence at Medium.
REPORTED_YIELD_TOLERANCE_PP = 0.25
MISMATCH_FLAG = "reported_yield_mismatch"
# Set when an exchange_reported (NSE OTC/RFQ) price is capped from High: the price basis is not
# verified, so High needs our YTM computed AND within tolerance of the NSE-reported yield.
BASIS_FLAG = "reported_basis_unverified"
CORP_TRADES_KIND = "exchange_reported"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compute_ytm_for(inst: sqlite3.Row, settlement, clean_price: float):
    """Return YtmResult or None when the inputs cannot be used."""
    if inst["face_value"] is None or inst["maturity_date"] is None:
        return None
    coupon = inst["coupon_pct"] if inst["coupon_pct"] is not None else 0.0
    if inst["coupon_frequency"] not in SINGLE_FLOW_FREQUENCIES and inst["coupon_pct"] is None:
        return None
    try:
        return compute_ytm(
            float(inst["face_value"]),
            float(coupon),
            inst["coupon_frequency"],
            _to_date(inst["maturity_date"]),
            settlement,
            clean_price,
            first_interest_date=_to_date(inst["first_interest_date"]) if inst["first_interest_date"] else None,
            issue_date=_to_date(inst["issue_date"]) if inst["issue_date"] else None,
        )
    except ValueError:
        return None


def run_screen(conn: sqlite3.Connection, settlement, run_id: str | None = None) -> dict:
    """Evaluate every instrument as of settlement and persist rejections and screen results.

    run_id defaults to 'screen-<settlement>', so a default run is also idempotent.
    Returns counts: {'run_id', 'settlement', 'instruments', 'buckets': {...},
    'rejections': {rule: n}, 'ytm_computed': n}.
    """
    settlement_d = _to_date(settlement)
    if run_id is None:
        run_id = f"screen-{settlement_d.isoformat()}"
    now = _now_iso()

    isins = [r["isin"] for r in conn.execute("SELECT isin FROM instrument ORDER BY isin")]

    # Replace this run's rows so a repeated run_id does not duplicate them.
    conn.execute("DELETE FROM rejection WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM screen_result WHERE run_id = ?", (run_id,))

    buckets = {"primary": 0, "speculative": 0, "rejected": 0}
    rules: dict[str, int] = {}
    ytm_computed = 0

    for isin in isins:
        result = evaluate(conn, isin, settlement_d, run_id)

        for rule, reason in result.failures:
            conn.execute(
                "INSERT INTO rejection (run_id, isin, rule, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, isin, rule, reason, now),
            )
            rules[rule] = rules.get(rule, 0) + 1

        bucket = result.bucket
        ytm = dirty = discount = None
        if result.bucket != "rejected" and result.price is not None and result.price.clean_price is not None:
            inst = conn.execute("SELECT * FROM instrument WHERE isin = ?", (isin,)).fetchone()
            res = _compute_ytm_for(inst, settlement_d, result.price.clean_price)
            if res is not None:
                dirty = res.dirty
                discount = res.discount_pct
                ytm = res.ytm
                if ytm is not None:
                    ytm_computed += 1
            confidence = result.confidence if ytm is not None else "Low"
            if ytm is None:
                # An independent YTM is required for any shortlist entry (SPEC integrity rules).
                reason = "YTM could not be computed from verified coupon/schedule/price"
                conn.execute(
                    "INSERT INTO rejection (run_id, isin, rule, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (run_id, isin, "ytm_not_computable", reason, now),
                )
                rules["ytm_not_computable"] = rules.get("ytm_not_computable", 0) + 1
                bucket = "rejected"
        else:
            confidence = result.confidence

        flags = list(result.flags)
        reported = result.price.reported_yield_pct if result.price else None
        vendor_ytm = result.price.vendor_ytm if result.price else None
        if vendor_ytm is None and reported is not None:
            # NSE-reported traded yield is shown in the vendor slot, unverified; never used as our YTM.
            vendor_ytm = reported
        within = (reported is not None and ytm is not None
                  and abs(ytm * 100.0 - reported) <= REPORTED_YIELD_TOLERANCE_PP)
        if reported is not None and ytm is not None and not within:
            flags.append(MISMATCH_FLAG)
        exchange = result.price is not None and result.price.kind == CORP_TRADES_KIND
        if exchange and confidence == "High" and not within:
            flags.append(BASIS_FLAG)
            confidence = "Medium"
        elif reported is not None and not within and confidence == "High":
            confidence = "Medium"

        conn.execute(
            "INSERT INTO screen_result (run_id, isin, bucket, ytm, dirty_price, discount_pct, "
            "next_interest_date, confidence, created_at, worst_rating, flags, clean_price, "
            "price_date, price_kind, vendor_ytm, settlement) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, isin, bucket, ytm, dirty, discount,
                result.next_interest_date.isoformat() if result.next_interest_date else None,
                confidence, now, result.worst_rating,
                ",".join(dict.fromkeys(flags)) or None,
                result.price.clean_price if result.price else None,
                result.price.price_date.isoformat() if result.price else None,
                result.price.kind if result.price else None,
                vendor_ytm,
                settlement_d.isoformat(),
            ),
        )
        buckets[bucket] += 1

    conn.commit()
    return {
        "run_id": run_id,
        "settlement": settlement_d.isoformat(),
        "instruments": len(isins),
        "buckets": buckets,
        "rejections": dict(sorted(rules.items())),
        "ytm_computed": ytm_computed,
    }
