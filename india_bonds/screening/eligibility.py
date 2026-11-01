"""Eligibility rules, rating normalisation and confidence for the bond screener.

evaluate() is read-only. screener.run_screen() persists rejection and
screen_result rows.

Rating rules (Phase 1):
- Per agency, the latest non-withdrawn action (by action_date, then id) counts.
  A withdrawn row is ignored, but an agency whose latest action is a withdrawal
  is flagged 'agency_withdrawn' so the reader can see it.
- The LOWEST rating across agencies decides the bucket (conservative).
- Issuer-level ratings do not exist in the schema and are never used.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import combinations

from dateutil.relativedelta import relativedelta

from india_bonds.analytics.cashflows import build_schedule, next_interest_date

STALE_PRICE_DAYS = 7
MAX_REMAINING_YEARS = 10
SINGLE_FLOW_FREQUENCIES = {"zero", "at_maturity"}
CORP_TRADES_KIND = "exchange_reported"  # NSE OTC/RFQ reported price, per 100 face

# Best to worst. Index is the notch rank (lower = better).
RATING_SCALE = [
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
    "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
    "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D",
]
RANK = {r: i for i, r in enumerate(RATING_SCALE)}
PRIMARY_MAX_RANK = RANK["AA-"]      # 3: AAA..AA- -> primary
SPECULATIVE_MAX_RANK = RANK["A"]    # 5: A+ and A -> speculative; A- and below rejected

_RATING_TOKEN = re.compile(r"^(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?|CC|C|D)$")
_PAREN = re.compile(r"\(([^()]*)\)")
_OUTLOOK_WORDS = re.compile(
    r"\b(STABLE|POSITIVE|NEGATIVE|DEVELOPING|RWN|RWP|RWD)\b"
)
_WATCH = re.compile(r"\bWATCH\b")
_SPLIT = re.compile(r"[\s/,]+")


@dataclass(frozen=True)
class ParsedRating:
    canonical: str
    rank: int
    flags: tuple[str, ...]


@dataclass(frozen=True)
class PriceQuote:
    snapshot_id: int
    price_date: date
    clean_price: float | None  # per face-value unit; None only for an unusable exchange_reported quote
    kind: str
    platform: str | None
    vendor_ytm: float | None  # comparison only; never used as our YTM
    reported_yield_pct: float | None = None  # NSE-reported traded yield (%), unverified
    basis: str = "per_face"  # 'per_face': stored clean price; 'per_100': NSE price per 100 face
    price_per_100: float | None = None  # exchange_reported only: NSE price per 100 face as stored


@dataclass(frozen=True)
class EligibilityResult:
    isin: str
    eligible: bool
    failures: list[tuple[str, str]]
    bucket: str  # 'primary' | 'speculative' | 'rejected'
    flags: list[str] = field(default_factory=list)
    confidence: str = "Low"  # 'High' | 'Medium' | 'Low'
    worst_rating: str | None = None
    price: PriceQuote | None = None
    next_interest_date: date | None = None


def parse_rating(text: str | None) -> ParsedRating | None:
    """Normalise a rating string such as 'CRISIL AA+/Stable' or 'AA (CE)'.

    Returns None when no single rating-scale token can be identified.
    Parenthetical (CE)/(SO) becomes flag 'credit_enhanced'; other qualifiers
    become 'rating_qualifier'; 'WATCH' becomes 'rating_watch'.
    """
    if text is None:
        return None
    s = text.upper().replace("–", "-").replace("—", "-").replace("*", " ")
    flags: list[str] = []
    for q in _PAREN.findall(s):
        q = q.strip()
        if not q or _OUTLOOK_WORDS.fullmatch(q):
            continue
        if _WATCH.search(q):
            flags.append("rating_watch")
            continue
        if q in ("CE", "SO"):
            flags.append("credit_enhanced")
        else:
            flags.append("rating_qualifier")
    s = _PAREN.sub(" ", s)
    if _WATCH.search(s):
        flags.append("rating_watch")
    s = _WATCH.sub(" ", s)
    s = _OUTLOOK_WORDS.sub(" ", s)
    tokens = [t for t in _SPLIT.split(s) if t]
    candidates = {t for t in tokens if _RATING_TOKEN.match(t)}
    if len(candidates) != 1:
        return None
    canonical = candidates.pop()
    return ParsedRating(canonical, RANK[canonical], tuple(dict.fromkeys(flags)))


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_date(value: str | None) -> date | None:
    """None for missing; ValueError for malformed."""
    if value is None or str(value).strip() == "":
        return None
    return date.fromisoformat(str(value).strip()[:10])


def latest_price(conn: sqlite3.Connection, isin: str, settlement: date) -> PriceQuote | None:
    """Latest price snapshot dated on/before settlement; the latest trade_date wins, tie -> larger id.

    - exchange_close / marketplace: clean_price is per face-value unit (basis 'per_face').
    - exchange_reported (NSE OTC/RFQ): price_per_100 is per 100 face. With face_value known,
      clean_price = price_per_100 * face_value / 100 (basis 'per_100'). With face_value NULL the
      quote is unusable (clean_price None). It is returned only when no usable quote exists,
      so eligibility can record 'unverified_face_value'.
    Snapshots with no usable price or unparseable dates are skipped.
    """
    face_row = conn.execute("SELECT face_value FROM instrument WHERE isin = ?", (isin,)).fetchone()
    face = float(face_row["face_value"]) if face_row is not None and face_row["face_value"] is not None else None
    rows = conn.execute(
        "SELECT id, trade_date, observed_at, clean_price, kind, platform, vendor_ytm, "
        "price_per_100, reported_yield_pct FROM price_snapshot "
        "WHERE isin = ? AND (clean_price IS NOT NULL OR price_per_100 IS NOT NULL)",
        (isin,),
    ).fetchall()
    best_usable: tuple[tuple[date, int], PriceQuote] | None = None
    best_unusable: tuple[tuple[date, int], PriceQuote] | None = None
    for r in rows:
        raw = r["trade_date"] or r["observed_at"]
        if not raw:
            continue
        try:
            d = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if d > settlement:
            continue
        key = (d, int(r["id"]))
        if r["kind"] == CORP_TRADES_KIND:
            p100 = r["price_per_100"]
            if p100 is None or p100 <= 0:
                continue
            clean = float(p100) * face / 100.0 if face is not None and face > 0 else None
            quote = PriceQuote(
                snapshot_id=int(r["id"]), price_date=d, clean_price=clean, kind=r["kind"],
                platform=r["platform"], vendor_ytm=None,
                reported_yield_pct=r["reported_yield_pct"], basis="per_100",
                price_per_100=float(p100),
            )
            if clean is None:
                if best_unusable is None or key > best_unusable[0]:
                    best_unusable = (key, quote)
                continue
        else:
            if r["clean_price"] is None or r["clean_price"] <= 0:
                continue
            quote = PriceQuote(
                snapshot_id=int(r["id"]), price_date=d, clean_price=float(r["clean_price"]),
                kind=r["kind"], platform=r["platform"], vendor_ytm=r["vendor_ytm"],
            )
        if best_usable is None or key > best_usable[0]:
            best_usable = (key, quote)
    if best_usable is not None:
        return best_usable[1]
    return best_unusable[1] if best_unusable is not None else None


def _current_and_withdrawn(rows) -> tuple[dict[str, sqlite3.Row], set[str]]:
    """Latest non-withdrawn row per agency, and agencies whose latest action is a withdrawal."""
    current: dict[str, tuple[tuple[str, int], sqlite3.Row]] = {}
    latest_any: dict[str, tuple[tuple[str, int], sqlite3.Row]] = {}
    for r in rows:
        agency = (r["agency"] or "").strip().upper()
        key = (r["action_date"] or "", int(r["id"]))
        if agency not in latest_any or key > latest_any[agency][0]:
            latest_any[agency] = (key, r)
        if (r["action"] or "") == "withdrawn":
            continue
        if agency not in current or key > current[agency][0]:
            current[agency] = (key, r)
    withdrawn = {a for a, (_, r) in latest_any.items() if (r["action"] or "") == "withdrawn"}
    return {a: r for a, (_, r) in current.items()}, withdrawn


def _schedule(inst: sqlite3.Row, maturity: date):
    """Return (schedule, None) or (None, reason)."""
    freq = inst["coupon_frequency"]
    if freq is None:
        return None, "coupon_frequency missing"
    if inst["face_value"] is None:
        return None, "face_value missing"
    if freq not in SINGLE_FLOW_FREQUENCIES and inst["coupon_pct"] is None:
        return None, "coupon_pct missing for periodic coupon"
    try:
        fid = _parse_date(inst["first_interest_date"])
        idate = _parse_date(inst["issue_date"])
    except ValueError as exc:
        return None, f"malformed schedule date: {exc}"
    if fid is None and idate is None:
        return None, "neither first_interest_date nor issue_date given"
    coupon = inst["coupon_pct"] if inst["coupon_pct"] is not None else 0.0
    try:
        sched = build_schedule(
            float(inst["face_value"]), float(coupon), freq, maturity,
            first_interest_date=fid, issue_date=idate,
        )
    except ValueError as exc:
        return None, f"schedule inconsistent: {exc}"
    return sched, None


def _master_complete(inst: sqlite3.Row) -> bool:
    required = (
        "face_value", "coupon_pct", "coupon_frequency", "maturity_date", "currency",
        "is_perpetual", "is_at1", "is_structured", "default_flag", "trading_suspended",
    )
    if any(inst[k] is None for k in required):
        return False
    return inst["first_interest_date"] is not None or inst["issue_date"] is not None


def _confidence(master: bool, has_rating: bool, price_ok: bool, exec_ok: bool, conflict: bool) -> str:
    if not master or not has_rating:
        return "Low"
    if price_ok and exec_ok:
        return "Medium" if conflict else "High"
    if price_ok or exec_ok:
        return "Medium"
    return "Low"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def evaluate(
    conn: sqlite3.Connection,
    isin: str,
    settlement,
    run_id: str | None = None,
) -> EligibilityResult:
    """Apply all eligibility rules for one ISIN as of settlement. Read-only.

    run_id is accepted for interface symmetry with the screener; it is not written here.
    """
    settlement = _to_date(settlement)
    failures: list[tuple[str, str]] = []
    flags: list[str] = []

    inst = conn.execute("SELECT * FROM instrument WHERE isin = ?", (isin,)).fetchone()
    if inst is None:
        return EligibilityResult(
            isin=isin, eligible=False,
            failures=[("no_active_isin", "instrument not in master")],
            bucket="rejected", confidence="Low",
        )

    if inst["exchange_listed"] != 1:
        failures.append(("no_active_isin", f"exchange_listed={inst['exchange_listed']!r}"))

    if inst["currency"] is None:
        failures.append(("unverified_currency", "currency NULL; not assumed INR"))
    elif inst["currency"] != "INR":
        failures.append(("not_inr", f"currency {inst['currency']}"))

    next_date: date | None = None
    try:
        maturity = _parse_date(inst["maturity_date"])
    except ValueError:
        maturity = None
        failures.append(("missing_coupon_or_schedule", "malformed maturity_date"))
    else:
        if maturity is None:
            failures.append(("missing_coupon_or_schedule", "maturity_date missing"))
        elif maturity <= settlement:
            failures.append(("matured", f"maturity {maturity} on or before settlement {settlement}"))
        else:
            if maturity > settlement + relativedelta(years=MAX_REMAINING_YEARS):
                failures.append((
                    "maturity_over_10y",
                    f"maturity {maturity} more than {MAX_REMAINING_YEARS} years after settlement",
                ))
            sched, reason = _schedule(inst, maturity)
            if reason is not None:
                failures.append(("missing_coupon_or_schedule", reason))
            else:
                next_date = next_interest_date(sched, settlement)

    for field_name, rule in (
        ("is_perpetual", "perpetual"),
        ("is_at1", "at1"),
        ("is_structured", "structured"),
        ("default_flag", "default_flag"),
        ("trading_suspended", "trading_suspended"),
    ):
        value = inst[field_name]
        if value is None:
            failures.append((f"unverified_{field_name}", f"{field_name} NULL; not assumed safe"))
        elif value == 1:
            failures.append((rule, f"{field_name} is true"))

    rating_rows = conn.execute("SELECT * FROM rating WHERE isin = ?", (isin,)).fetchall()
    current, withdrawn = _current_and_withdrawn(rating_rows)
    if withdrawn:
        flags.append("agency_withdrawn")
    if not current:
        failures.append(("no_instrument_rating", "no non-withdrawn instrument rating"))

    parsed: list[tuple[str, ParsedRating]] = []
    for agency in sorted(current):
        row = current[agency]
        p = parse_rating(row["rating"])
        if p is None:
            failures.append(("unparseable_rating", f"{agency}: {row['rating']!r}"))
        else:
            parsed.append((agency, p))
            flags.extend(p.flags)

    worst: ParsedRating | None = None
    if parsed:
        worst = max((p for _, p in parsed), key=lambda p: p.rank)
        if worst.rank > SPECULATIVE_MAX_RANK:
            failures.append(("rating_below_A", f"lowest rating {worst.canonical} is below A"))
        for (_, p1), (_, p2) in combinations(parsed, 2):
            lo, hi = sorted((p1.rank, p2.rank))
            if hi - lo >= 2 or (lo <= PRIMARY_MAX_RANK < hi):
                flags.append("rating_conflict")

    quote = latest_price(conn, isin, settlement)
    if quote is None:
        failures.append(("stale_price", "no exchange_close/marketplace price on or before settlement; not price-verified"))
    elif quote.clean_price is None:
        failures.append((
            "unverified_face_value",
            "NSE reported price is per 100 face but face_value is NULL; cannot convert to a per-face price",
        ))
    elif (settlement - quote.price_date).days > STALE_PRICE_DAYS:
        failures.append((
            "stale_price",
            f"last price {quote.price_date} is more than {STALE_PRICE_DAYS} days before settlement; not price-verified",
        ))
    price_ok = (quote is not None and quote.clean_price is not None
                and (settlement - quote.price_date).days <= STALE_PRICE_DAYS)

    # Executable verification is positive evidence: a later unverified row (e.g. a routine
    # price ingest) must not erase it. Valid while observed within STALE_PRICE_DAYS of settlement.
    exec_ok = False
    for row in conn.execute(
        "SELECT observed_at FROM availability WHERE isin = ? AND executable_verified = 1", (isin,)
    ):
        try:
            seen = _to_date((row["observed_at"] or "")[:10])
        except (ValueError, TypeError):
            continue
        if seen <= settlement and (settlement - seen).days <= STALE_PRICE_DAYS:
            exec_ok = True
            break

    conflict = "rating_conflict" in flags
    confidence = _confidence(_master_complete(inst), worst is not None, price_ok, exec_ok, conflict)

    if failures:
        bucket = "rejected"
    elif worst.rank <= PRIMARY_MAX_RANK:
        bucket = "primary"
    else:
        bucket = "speculative"

    return EligibilityResult(
        isin=isin,
        eligible=not failures,
        failures=failures,
        bucket=bucket,
        flags=_dedupe(flags),
        confidence=confidence,
        worst_rating=worst.canonical if worst else None,
        price=quote,
        next_interest_date=next_date,
    )
