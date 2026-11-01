"""Tests for the BondDekho text parser. All fixtures are SYNTHETIC (made-up issuers/ISINs)."""

from datetime import date

from india_bonds.enrichment.bonddekho import BondDekhoRecord, parse_bonddekho


def _join(*lines: str) -> str:
    return "\n".join(lines) + "\n"


NORMAL = _join(
    "## Alpha Testco Finance Limited — INE000T01001",
    "",
    "- **Type:** Senior Secured Bond",
    "- **Credit Rating:** A (CRISIL)",
    "- **Coupon Rate:** 10.3%",
    "- **Coupon Frequency:** Monthly",
    "- **Face Value:** Rs. 1000.0",
    "- **Maturity Date:** 2028-05-31 (1.6 years to maturity)",
    "- **Issue Date:** 2026-04-21",
    "- **Sector:** NBFC",
    "- **Security Type:** Senior Secured",
    "",
    "### Available on 2 platform(s):",
    "",
    "- **IndiaBonds:** 10.50% YTM, Price: Rs. 10,072, Min: N/A",
    "- **WintWealth:** 10.35% YTM, Price: Rs. 10,003, Min: Rs. 10,000",
    "",
    "View on BondDekho: https://www.bonddekho.com/bond/INE000T01001",
)

NOT_FOUND = _join(
    "Bond with ISIN 'INE000T09999' not found.",
    "",
    "Disclaimer: BondDekho is a data aggregation platform.",
)


def test_normal_record_fields():
    rec = parse_bonddekho(NORMAL)
    assert isinstance(rec, BondDekhoRecord)
    assert rec.found is True
    assert rec.isin == "INE000T01001"
    assert rec.issuer == "Alpha Testco Finance Limited"
    assert rec.bond_type == "Senior Secured Bond"
    assert rec.rating == "A"
    assert rec.rating_agency == "CRISIL"
    assert rec.coupon_pct == 10.3
    assert rec.coupon_frequency_raw == "Monthly"
    assert rec.coupon_frequency == "monthly"
    assert rec.face_value == 1000.0
    assert rec.maturity_date == date(2028, 5, 31)
    assert rec.issue_date == date(2026, 4, 21)
    assert rec.sector == "NBFC"
    assert rec.security_type_raw == "Senior Secured"
    assert rec.secured is True
    assert rec.seniority == "senior"
    assert rec.platforms == [
        {"platform": "IndiaBonds", "ytm_pct": 10.5, "price": 10072.0, "min_investment": None},
        {"platform": "WintWealth", "ytm_pct": 10.35, "price": 10003.0, "min_investment": 10000.0},
    ]
    assert rec.warnings == []


def test_rating_agency_none_word_becomes_none():
    text = NORMAL.replace("**Credit Rating:** A (CRISIL)", "**Credit Rating:** A (None)")
    rec = parse_bonddekho(text)
    assert rec.found is True
    assert rec.rating == "A"
    assert rec.rating_agency is None
    assert "rating_agency_none" in rec.warnings


def test_garbled_frequency_is_not_normalised():
    text = NORMAL.replace("**Coupon Frequency:** Monthly", "**Coupon Frequency:** on 29 Nov 2026")
    rec = parse_bonddekho(text)
    assert rec.coupon_frequency is None
    assert rec.coupon_frequency_raw == "on 29 Nov 2026"
    assert "coupon_frequency_unparsed" in rec.warnings


def test_frequency_synonyms_normalise():
    cases = {
        "QUARTERLY": "quarterly",
        "SEMI ANNUALLY": "semiannual",
        "Annually": "annual",
        "At Maturity": "at_maturity",
        "None": None,
    }
    for raw, expected in cases.items():
        text = NORMAL.replace("**Coupon Frequency:** Monthly", f"**Coupon Frequency:** {raw}")
        rec = parse_bonddekho(text)
        assert rec.coupon_frequency == expected, raw
        assert rec.coupon_frequency_raw == raw


def test_security_type_none_gives_unknown_secured():
    text = NORMAL.replace("**Security Type:** Senior Secured", "**Security Type:** None")
    rec = parse_bonddekho(text)
    assert rec.security_type_raw is None
    assert rec.secured is None
    # 'Senior' still appears on the Type line, so seniority comes from there.
    assert rec.seniority == "senior"
    assert "security_type_none" in rec.warnings

    no_seniority_words = text.replace("**Type:** Senior Secured Bond", "**Type:** NCD")
    assert parse_bonddekho(no_seniority_words).seniority is None


def test_unsecured_subordinated():
    text = NORMAL.replace("**Security Type:** Senior Secured", "**Security Type:** Unsecured Subordinated Tier 2")
    rec = parse_bonddekho(text)
    assert rec.secured is False
    assert rec.seniority == "subordinated"


def test_no_platform_listings():
    text = _join(
        "## Beta Synthetic Credit Private Limited — INE000T01002",
        "",
        "- **Type:** NCD",
        "- **Credit Rating:** BBB- (None)",
        "- **Coupon Rate:** N/A",
        "- **Coupon Frequency:** None",
        "- **Face Value:** Rs. 10000.0",
        "- **Maturity Date:** 2027-11-27 (1.1 years to maturity)",
        "- **Issue Date:** None",
        "- **Sector:** None",
        "- **Security Type:** None",
        "",
        "No platform listings currently available.",
    )
    rec = parse_bonddekho(text)
    assert rec.found is True
    assert rec.platforms == []
    assert rec.coupon_pct is None
    assert rec.issue_date is None
    assert rec.sector is None
    assert rec.rating == "BBB-"
    assert rec.rating_agency is None


def test_not_found():
    rec = parse_bonddekho(NOT_FOUND)
    assert rec.found is False
    assert rec.isin == "INE000T09999"
    assert rec.platforms == []
    assert rec.warnings == ["not_found"]


def test_odd_numbers_with_commas():
    text = _join(
        "## Gamma Testfin Limited — INE000T01003",
        "",
        "- **Type:** NCD",
        "- **Credit Rating:** AA- (ICRA)",
        "- **Coupon Rate:** 12.95%",
        "- **Coupon Frequency:** Monthly",
        "- **Face Value:** Rs. 100,000.0",
        "- **Maturity Date:** 2029-01-30 (2.2 years to maturity)",
        "- **Issue Date:** None",
        "- **Sector:** Power",
        "- **Security Type:** Secured",
        "",
        "### Available on 1 platform(s):",
        "",
        "- **GoldenPi:** 12.50% YTM, Price: Rs. 1,02,000.5, Min: Rs. 101,621",
    )
    rec = parse_bonddekho(text)
    assert rec.face_value == 100000.0
    assert rec.coupon_pct == 12.95
    assert rec.platforms[0]["price"] == 102000.5
    assert rec.platforms[0]["min_investment"] == 101621.0
    assert rec.warnings == []


def test_platform_na_values():
    text = _join(
        "## Delta Synthetic Finance Limited — INE000T01004",
        "",
        "- **Credit Rating:** A+ (IND)",
        "- **Coupon Rate:** 9.0%",
        "- **Coupon Frequency:** Monthly",
        "- **Face Value:** Rs. 1000.0",
        "- **Maturity Date:** 2028-02-19 (1.3 years to maturity)",
        "- **Security Type:** Secured",
        "",
        "### Available on 1 platform(s):",
        "",
        "- **TheFixedIncome:** N/A YTM, Price: N/A, Min: N/A",
    )
    rec = parse_bonddekho(text)
    assert rec.platforms == [
        {"platform": "TheFixedIncome", "ytm_pct": None, "price": None, "min_investment": None}
    ]


def test_missing_fields_are_none_and_warned():
    text = _join(
        "## Epsilon Testco Limited — INE000T01005",
        "",
        "- **Credit Rating:** A (ACUITE)",
        "- **Coupon Rate:** 10.0%",
        "- **Coupon Frequency:** Quarterly",
        "- **Face Value:** Rs. 1000.0",
        "- **Security Type:** Secured",
    )
    rec = parse_bonddekho(text)
    assert rec.found is True
    assert rec.maturity_date is None
    assert rec.issue_date is None
    assert rec.sector is None
    assert rec.bond_type is None
    assert "missing_field:Maturity Date" in rec.warnings
    assert rec.coupon_frequency == "quarterly"


def test_unparseable_maturity_is_none_with_warning():
    text = NORMAL.replace("**Maturity Date:** 2028-05-31 (1.6 years to maturity)", "**Maturity Date:** 31/05/2028")
    rec = parse_bonddekho(text)
    assert rec.maturity_date is None
    assert "maturity_date_unparsed" in rec.warnings


def test_platform_count_mismatch_warned():
    text = NORMAL.replace("Available on 2 platform(s)", "Available on 3 platform(s)")
    rec = parse_bonddekho(text)
    assert "platform_count_mismatch" in rec.warnings


def test_never_raises_on_bad_input():
    for bad in ["", "   ", None, 12345, "random garbage\n- **x** y", "## only a header"]:
        rec = parse_bonddekho(bad)  # type: ignore[arg-type]
        assert isinstance(rec, BondDekhoRecord)
        assert rec.found is False
