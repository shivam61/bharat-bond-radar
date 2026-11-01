"""Pure parser for BondDekho MCP `get_bond` text output.

BondDekho is a marketplace aggregator. Every value parsed here is a third-party
UNVERIFIED hint: nothing in this module is written to master data. The parser
never raises; malformed or missing fields become None and are recorded in
`warnings`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

_NONE_WORDS = {"none", "n/a", ""}

_HEADER_RE = re.compile(
    r"^##\s+(?P<issuer>.+?)\s+[—–-]\s+(?P<isin>IN[A-Z0-9]{10})\s*$", re.MULTILINE
)
_NOT_FOUND_RE = re.compile(r"Bond with ISIN '([^']*)' not found", re.IGNORECASE)
_FIELD_RE = re.compile(r"^-\s+\*\*(?P<label>[^*]+?):\*\*\s*(?P<value>.*?)\s*$")
_RATING_RE = re.compile(r"^(?P<rating>.*?)\s*\((?P<agency>[^()]*)\)\s*$")
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_PLATFORM_RE = re.compile(
    r"^(?P<ytm>.+?)\s*YTM,\s*Price:\s*(?P<price>.+?),\s*Min:\s*(?P<min>.+?)\s*$"
)
_FREQ_MAP = {
    "monthly": "monthly",
    "quarterly": "quarterly",
    "semiannual": "semiannual",
    "semi-annual": "semiannual",
    "semi annual": "semiannual",
    "semiannually": "semiannual",
    "semi-annually": "semiannual",
    "semi annually": "semiannual",
    "half-yearly": "semiannual",
    "annual": "annual",
    "annually": "annual",
    "yearly": "annual",
    "at maturity": "at_maturity",
}


@dataclass(frozen=True)
class BondDekhoRecord:
    found: bool
    isin: str | None = None
    issuer: str | None = None
    bond_type: str | None = None
    rating: str | None = None
    rating_agency: str | None = None
    coupon_pct: float | None = None
    coupon_frequency_raw: str | None = None
    coupon_frequency: str | None = None
    face_value: float | None = None
    maturity_date: date | None = None
    issue_date: date | None = None
    sector: str | None = None
    security_type_raw: str | None = None
    secured: bool | None = None
    seniority: str | None = None
    platforms: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_none(value: str | None) -> bool:
    return value is None or value.strip().lower() in _NONE_WORDS


def _to_float(value: str | None) -> float | None:
    """Parse '10.3%', 'Rs. 1,000.0', '100000.0' to float; None when absent/unparseable."""
    if _is_none(value):
        return None
    match = _NUM_RE.search(value.replace("%", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _to_date(value: str | None) -> date | None:
    if _is_none(value):
        return None
    match = _DATE_RE.search(value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _normalise_frequency(raw: str | None) -> str | None:
    if _is_none(raw):
        return None
    return _FREQ_MAP.get(raw.strip().lower())


def _parse_rating(value: str | None, warnings: list[str]) -> tuple[str | None, str | None]:
    if _is_none(value):
        return None, None
    match = _RATING_RE.match(value)
    if not match:
        warnings.append("rating_agency_missing")
        return value.strip(), None
    rating = match.group("rating").strip() or None
    agency_raw = match.group("agency").strip()
    if _is_none(agency_raw):
        warnings.append("rating_agency_none")
        agency = None
    else:
        agency = agency_raw
    if _is_none(rating):
        rating = None
    return rating, agency


def _parse_secured(security_raw: str | None) -> bool | None:
    if _is_none(security_raw):
        return None
    lowered = security_raw.lower()
    if re.search(r"\bunsecured\b", lowered):
        return False
    if re.search(r"\bsecured\b", lowered):
        return True
    return None


def _parse_seniority(*texts: str | None) -> str | None:
    joined = " ".join(t for t in texts if not _is_none(t))
    if re.search(r"\bsubordinated\b", joined, re.IGNORECASE):
        return "subordinated"
    if re.search(r"\bsenior\b", joined, re.IGNORECASE):
        return "senior"
    return None


def _parse_platforms(lines: list[str], warnings: list[str]) -> list[dict]:
    platforms: list[dict] = []
    for line in lines:
        match = _FIELD_RE.match(line)
        if not match:
            continue
        name = match.group("label").strip()
        body = match.group("value").strip()
        parsed = _PLATFORM_RE.match(body)
        if not parsed:
            warnings.append(f"platform_line_unparsed:{name}")
            continue
        platforms.append(
            {
                "platform": name,
                "ytm_pct": _to_float(parsed.group("ytm")),
                "price": _to_float(parsed.group("price")),
                "min_investment": _to_float(parsed.group("min")),
            }
        )
    return platforms


def parse_bonddekho(text: str) -> BondDekhoRecord:
    """Parse BondDekho `get_bond` text. Never raises."""
    try:
        return _parse(text)
    except Exception as exc:  # defensive: the contract is "never raises"
        return BondDekhoRecord(found=False, warnings=[f"parse_error:{type(exc).__name__}"])


def _parse(text: str) -> BondDekhoRecord:
    if not isinstance(text, str) or not text.strip():
        return BondDekhoRecord(found=False, warnings=["empty_text"])

    not_found = _NOT_FOUND_RE.search(text)
    header = _HEADER_RE.search(text)
    if not_found and not header:
        return BondDekhoRecord(found=False, isin=not_found.group(1), warnings=["not_found"])
    if not header:
        return BondDekhoRecord(found=False, warnings=["unrecognised_format"])

    warnings: list[str] = []
    lines = text.splitlines()

    # Split: field block sits before the platform section.
    split_idx = len(lines)
    for idx, line in enumerate(lines):
        if line.startswith("###") or line.startswith("No platform listings"):
            split_idx = idx
            break
    field_lines = lines[:split_idx]
    platform_lines = lines[split_idx:]

    values: dict[str, str] = {}
    for line in field_lines:
        match = _FIELD_RE.match(line)
        if match:
            label = match.group("label").strip()
            values.setdefault(label, match.group("value").strip())

    required = ["Credit Rating", "Coupon Rate", "Coupon Frequency", "Face Value", "Maturity Date", "Security Type"]
    for label in required:
        if label not in values:
            warnings.append(f"missing_field:{label}")

    rating, agency = _parse_rating(values.get("Credit Rating"), warnings)

    coupon_raw = values.get("Coupon Rate")
    coupon_pct = _to_float(coupon_raw)
    if not _is_none(coupon_raw) and coupon_pct is None:
        warnings.append("coupon_rate_unparsed")

    freq_raw = values.get("Coupon Frequency")
    freq = _normalise_frequency(freq_raw)
    if not _is_none(freq_raw) and freq is None:
        warnings.append("coupon_frequency_unparsed")

    face_raw = values.get("Face Value")
    face_value = _to_float(face_raw)
    if not _is_none(face_raw) and face_value is None:
        warnings.append("face_value_unparsed")

    maturity_raw = values.get("Maturity Date")
    maturity = _to_date(maturity_raw)
    if not _is_none(maturity_raw) and maturity is None:
        warnings.append("maturity_date_unparsed")

    issue_raw = values.get("Issue Date")
    issue = _to_date(issue_raw)
    if not _is_none(issue_raw) and issue is None:
        warnings.append("issue_date_unparsed")

    security_raw = values.get("Security Type")
    if _is_none(security_raw):
        warnings.append("security_type_none")
        security_raw_out = None
    else:
        security_raw_out = security_raw

    bond_type_raw = values.get("Type")
    bond_type = None if _is_none(bond_type_raw) else bond_type_raw

    sector_raw = values.get("Sector")
    sector = None if _is_none(sector_raw) else sector_raw

    platforms = _parse_platforms(platform_lines, warnings)
    available = re.search(r"Available on (\d+) platform", text)
    if available and int(available.group(1)) != len(platforms):
        warnings.append("platform_count_mismatch")

    issuer = header.group("issuer").strip()
    return BondDekhoRecord(
        found=True,
        isin=header.group("isin"),
        issuer=issuer or None,
        bond_type=bond_type,
        rating=rating,
        rating_agency=agency,
        coupon_pct=coupon_pct,
        coupon_frequency_raw=freq_raw,
        coupon_frequency=freq,
        face_value=face_value,
        maturity_date=maturity,
        issue_date=issue,
        sector=sector,
        security_type_raw=security_raw_out,
        secured=_parse_secured(security_raw),
        seniority=_parse_seniority(bond_type_raw, security_raw),
        platforms=platforms,
        warnings=warnings,
    )
