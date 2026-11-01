"""Parse NSE bond descriptor strings into DRAFT, UNVERIFIED hints.

Everything returned here is a draft hint used only to cross-check a primary
source. Descriptor dates and coupons can be wrong or stale (e.g. Muthoot MCRED
descriptor 28FB27 vs CDSL redemption 08-Mar-2027), so these values must never
be written as verified master values. Pure function: no I/O, never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# Two-letter month codes. The corpus contains exactly 12 distinct codes
# (FB MR AP MY JU JL AG SP OT NV DC JN), so they must map bijectively to the
# 12 months. JN is January and JU is June, from month-length evidence:
# JN carries day 31 (31JN27, 31JN29) so it cannot be June, while JU never
# exceeds day 30. JA is not used: it would be a second code for January.
MONTH_CODES: dict[str, int] = {
    "JN": 1,
    "FB": 2,
    "MR": 3,
    "AP": 4,
    "MY": 5,
    "JU": 6,
    "JL": 7,
    "AG": 8,
    "SP": 9,
    "OT": 10,
    "NV": 11,
    "DC": 12,
}

# Spelled-out three-letter month abbreviations (e.g. 07MAY2026, 26SEP27).
MONTH_ALIASES: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

# Instrument kind tokens seen in the corpus (NCD, BD, LOA, PTC, STRPP) plus
# the other common NSE kinds that are not observed in the local data.
KIND_TOKENS: tuple[str, ...] = (
    "NCD",
    "BD",
    "DEB",
    "PPD",
    "TFB",
    "LOA",
    "ZCB",
    "STRPP",
    "PTC",
)

FACE_VALUE_UNITS: dict[str | None, int] = {None: 1, "LAC": 100_000, "CR": 10_000_000}

_UPTO_RE = re.compile(r"LOA\s*UPTO\s*\S+")
_GLUE_KIND_RE = re.compile(r"(%s)(?=\d)" % "|".join(KIND_TOKENS))
_KIND_RE = re.compile(r"(?<![A-Z])(%s)(?![A-Z])" % "|".join(KIND_TOKENS))
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})([A-Z]{2,4})(\d{4}|\d{2})(?!\d)")
_MALFORMED_DATE_RE = re.compile(r"(?<!\d)\d{1,2}[A-Z]{2,4}\d+")
_FV_RE = re.compile(r"FVRS\s*(\d+(?:\.\d+)?)\s*(LAC|CR)?")
_COUPON_TAIL_RE = re.compile(r"(?:^|\s)(\d+(?:\.\d+)?%?)\s*$")
_DECIMAL_OR_PCT_RE = re.compile(r"(?<!\S)(\d+\.\d+%?|\d+%)(?!\S)")
_SERIES_RE = re.compile(r"(?<![A-Z])SR(?![A-Z])")
_TR_RE = re.compile(r"(?<![A-Z])TR(?![A-Z])")
_PERP_RE = re.compile(r"(?<![A-Z])PERP(?:ETUAL)?(?![A-Z])")
_AT1_RE = re.compile(r"(?<![A-Z0-9])AT1(?![A-Z0-9])")
_CALL_RE = re.compile(r"(?<![A-Z])CALL(?![A-Z])")
_MAX_PLAUSIBLE_COUPON = 100.0
_TRAILING_NUMBER_RE = re.compile(r"\s+\d+(?:\.\d+)?%?$")


@dataclass(frozen=True)
class Descriptor:
    """Draft facts from one descriptor string. Every value is UNVERIFIED."""

    raw: str
    issuer: str | None
    series: str | None
    coupon_pct: float | None
    instrument_kind: str | None
    maturity_date: date | None
    face_value: float | None
    perpetual_hint: bool
    at1_hint: bool
    call_hint: bool
    warnings: list[str] = field(default_factory=list)


def _empty(raw: str, warnings: list[str]) -> Descriptor:
    return Descriptor(
        raw=raw,
        issuer=None,
        series=None,
        coupon_pct=None,
        instrument_kind=None,
        maturity_date=None,
        face_value=None,
        perpetual_hint=False,
        at1_hint=False,
        call_hint=False,
        warnings=warnings,
    )


def _parse_face_value(s: str, warnings: list[str]) -> float | None:
    if "FVRS" not in s:
        warnings.append("missing face value")
        return None
    amounts: set[float] = set()
    for m in _FV_RE.finditer(s):
        amounts.add(float(m.group(1)) * FACE_VALUE_UNITS[m.group(2)])
    if not amounts:
        warnings.append("face value token without amount")
        return None
    if len(amounts) > 1:
        warnings.append("ambiguous face value")
        return None
    return amounts.pop()


def _parse_maturity(s: str, warnings: list[str]) -> date | None:
    for m in _MALFORMED_DATE_RE.finditer(s):
        if not _DATE_RE.fullmatch(m.group(0)):
            warnings.append(f"malformed date token {m.group(0)}")

    valid: set[date] = set()
    unknown = False
    for m in _DATE_RE.finditer(s):
        day_s, code, year_s = m.groups()
        month = MONTH_CODES.get(code) or MONTH_ALIASES.get(code)
        if month is None:
            unknown = True
            warnings.append(f"unknown month code {code}")
            continue
        year = int(year_s) + (2000 if len(year_s) == 2 else 0)
        try:
            valid.add(date(year, month, int(day_s)))
        except ValueError:
            warnings.append(f"invalid date {m.group(0)}")
    if unknown:
        return None
    if len(valid) > 1:
        warnings.append("ambiguous maturity dates")
        return None
    return next(iter(valid)) if valid else None


def parse_descriptor(text: str) -> Descriptor:
    """Parse an NSE descriptor string into draft, unverified facts. Never raises."""
    if not isinstance(text, str):
        return _empty("", ["not a string"])
    raw = text.strip()
    if not raw:
        return _empty(raw, ["empty descriptor"])

    s = raw.upper()
    s = _UPTO_RE.sub(" ", s)  # LOA UPTO <date> is a different date; drop it
    s = _GLUE_KIND_RE.sub(r"\1 ", s)
    s = re.sub(r"\s+", " ", s)

    warnings: list[str] = []

    kind_match = _KIND_RE.search(s)
    kind = kind_match.group(1) if kind_match else None
    kind_start = kind_match.start() if kind_match else None
    if kind is None:
        warnings.append("no instrument kind token")

    _SERIES_WORDS = frozenset({"SR", "SERIES", "TR", "TRANCHE", "OPTION", "OPT"})

    # Coupon: the numeric token immediately before the kind token.
    coupon: float | None = None
    coupon_start: int | None = None
    if kind_start is not None:
        pre = s[:kind_start].rstrip()
        tail = _COUPON_TAIL_RE.search(pre)
        if tail is None:
            warnings.append("coupon not immediately before kind token")
        else:
            chosen = tail.group(1)
            coupon_start = tail.start(1)
            prev_words = pre[:coupon_start].split()
            if prev_words and prev_words[-1].upper() in _SERIES_WORDS:
                # e.g. "ZETA FIN SR 2 BD 31OT31": the number is a series/tranche id, not a coupon.
                warnings.append(f"numeric token {chosen} follows {prev_words[-1]}: series number, not coupon")
                chosen = None
            others = [
                t for t in _DECIMAL_OR_PCT_RE.findall(pre) if t != chosen
            ] if chosen is not None else []
            if chosen is None:
                pass
            elif others:
                warnings.append(
                    "ambiguous coupon candidates " + ", ".join([chosen, *others])
                )
            else:
                value = float(chosen.rstrip("%"))
                if value > _MAX_PLAUSIBLE_COUPON:
                    warnings.append(f"implausible coupon {chosen}")
                else:
                    coupon = value
    else:
        warnings.append("coupon not determined: no kind token")

    maturity = _parse_maturity(s, warnings)
    face = _parse_face_value(s, warnings)

    if maturity is None and not any(
        w.startswith(("unknown month", "invalid date", "ambiguous maturity", "malformed date"))
        for w in warnings
    ):
        warnings.append("missing maturity date")

    # Series: from the SR token up to the coupon, kind, or face-value token.
    series: str | None = None
    series_match = _SERIES_RE.search(s)
    if series_match:
        ends = [len(s)]
        for pos in (coupon_start, kind_start, s.find("FVRS")):
            if pos is not None and pos >= 0 and pos > series_match.start():
                ends.append(pos)
        series = s[series_match.start() : min(ends)].strip() or None

    # Issuer: text before the first series, TR, coupon, kind, or FVRS token.
    cuts = [len(s)]
    for pos in (
        series_match.start() if series_match else None,
        (_TR_RE.search(s).start() if _TR_RE.search(s) else None),
        coupon_start,
        kind_start,
        s.find("FVRS") if "FVRS" in s else None,
    ):
        if pos is not None and pos >= 0:
            cuts.append(pos)
    issuer = _TRAILING_NUMBER_RE.sub("", s[: min(cuts)].strip()).strip(" -,") or None
    if issuer is None:
        warnings.append("missing issuer")

    warnings = list(dict.fromkeys(warnings))  # drop repeats, keep order

    return Descriptor(
        raw=raw,
        issuer=issuer,
        series=series,
        coupon_pct=coupon,
        instrument_kind=kind,
        maturity_date=maturity,
        face_value=face,
        perpetual_hint=bool(_PERP_RE.search(s)),
        at1_hint=bool(_AT1_RE.search(s)),
        call_hint=bool(_CALL_RE.search(s)),
        warnings=warnings,
    )
