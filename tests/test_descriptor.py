"""Tests for india_bonds.enrichment.descriptor. Expected values are hand-derived from the strings.

Results are DRAFT hints. These tests never touch data/bonds.db; the corpus test
reads only tests/fixtures/nse_descriptors.txt.
"""

import dataclasses
import random
import re
from datetime import date
from pathlib import Path

import pytest

from india_bonds.enrichment.descriptor import (
    MONTH_CODES,
    Descriptor,
    parse_descriptor,
)

FIXTURE = Path(__file__).parent / "fixtures" / "nse_descriptors.txt"


# --- The ten real examples from the spec (trailing '\r' as in the NSE feed) ---

SPEC_EXAMPLES = [
    (
        "REC LIMITED SR 261 8.3 BD 31OT31 FVRS1LAC",
        dict(issuer="REC LIMITED", series="SR 261", coupon_pct=8.3, instrument_kind="BD",
             maturity_date=date(2031, 10, 31), face_value=100000.0),
    ),
    (
        "NATIONAL BANK FOR AGRICULTURE AND RURAL DEVELOPMENT SR 26F 7.44 BD 17JL29 FVRS1LAC",
        dict(issuer="NATIONAL BANK FOR AGRICULTURE AND RURAL DEVELOPMENT", series="SR 26F",
             coupon_pct=7.44, instrument_kind="BD", maturity_date=date(2029, 7, 17),
             face_value=100000.0),
    ),
    (
        "NAVI FINSERV LIMITED SR C 10.30 NCD 31MY28 FVRS10000",
        dict(issuer="NAVI FINSERV LIMITED", series="SR C", coupon_pct=10.3,
             instrument_kind="NCD", maturity_date=date(2028, 5, 31), face_value=10000.0),
    ),
    (
        "IIFL SAMASTA FINANCE LIMITED 9.20 NCD 30DC28 FVRS10000",
        dict(issuer="IIFL SAMASTA FINANCE LIMITED", series=None, coupon_pct=9.2,
             instrument_kind="NCD", maturity_date=date(2028, 12, 30), face_value=10000.0),
    ),
    (
        "KEERTANA FINSERV LIMITED 11.7 NCD 16DC28 FVRS10000",
        dict(issuer="KEERTANA FINSERV LIMITED", series=None, coupon_pct=11.7,
             instrument_kind="NCD", maturity_date=date(2028, 12, 16), face_value=10000.0),
    ),
    (
        "MUTHOOT MCRED LIMITED SR I 9 NCD 28FB27 FVRS1000",
        dict(issuer="MUTHOOT MCRED LIMITED", series="SR I", coupon_pct=9.0,
             instrument_kind="NCD", maturity_date=date(2027, 2, 28), face_value=1000.0),
    ),
    (
        "AYE FINANCE LIMITED 9.75 NCD 28JL28 FVRS1LAC",
        dict(issuer="AYE FINANCE LIMITED", series=None, coupon_pct=9.75,
             instrument_kind="NCD", maturity_date=date(2028, 7, 28), face_value=100000.0),
    ),
    (
        "REGENCY FINCORP LIMITED SR 1 TR XIV 12 NCD 09AP28 FVRS10000",
        dict(issuer="REGENCY FINCORP LIMITED", series="SR 1 TR XIV", coupon_pct=12.0,
             instrument_kind="NCD", maturity_date=date(2028, 4, 9), face_value=10000.0),
    ),
    (
        "PROGFIN PRIVATE LIMITED 11 NCD 13JN27 FVRS10000",
        dict(issuer="PROGFIN PRIVATE LIMITED", series=None, coupon_pct=11.0,
             instrument_kind="NCD", maturity_date=date(2027, 1, 13), face_value=10000.0),
    ),
    (
        "CHAIN-N-CHAINS JEWELS LIMITED 12 NCD 22OT28 FVRS10000",
        dict(issuer="CHAIN-N-CHAINS JEWELS LIMITED", series=None, coupon_pct=12.0,
             instrument_kind="NCD", maturity_date=date(2028, 10, 22), face_value=10000.0),
    ),
]


@pytest.mark.parametrize("text,expected", SPEC_EXAMPLES, ids=[t[:24] for t, _ in SPEC_EXAMPLES])
def test_spec_examples_exact(text, expected):
    d = parse_descriptor(text + "\r")  # feed strings carry a trailing '\r'
    assert d.raw == text
    for key, value in expected.items():
        assert getattr(d, key) == value, key
    assert d.warnings == []
    assert d.perpetual_hint is False
    assert d.at1_hint is False
    assert d.call_hint is False


def test_muthoot_mcred_is_draft_only():
    # CDSL reports 08-Mar-2027 as the actual redemption date; the descriptor says 28-Feb-2027.
    # The parser reports the descriptor date as a draft hint and never overrides it.
    d = parse_descriptor("MUTHOOT MCRED LIMITED SR I 9 NCD 28FB27 FVRS1000")
    assert d.maturity_date == date(2027, 2, 28)
    assert d.maturity_date != date(2027, 3, 8)


# --- Month-code table ---

def test_month_codes_are_a_bijection_onto_twelve_months():
    assert sorted(MONTH_CODES.values()) == list(range(1, 13))
    assert len(MONTH_CODES) == 12


@pytest.mark.parametrize(
    "text,expected",
    [
        ("A NCD 01JN28 FVRS1LAC", date(2028, 1, 1)),   # JN = January (carries day 31 in corpus)
        ("A NCD 16JU31 FVRS1LAC", date(2031, 6, 16)),  # JU = June (never exceeds day 30)
        ("A NCD 31JN29 FVRS10000", date(2029, 1, 31)),
        ("A NCD 28FB27 FVRS1000", date(2027, 2, 28)),
        ("A NCD 10SP27 FVRS1LAC", date(2027, 9, 10)),
        ("A NCD 31OT31 FVRS1LAC", date(2031, 10, 31)),
        ("A NCD 30NV27 FVRS1000", date(2027, 11, 30)),
        ("A NCD 18DC29 FVRS1LAC", date(2029, 12, 18)),
        ("A NCD 07AP30 FVRS1LAC", date(2030, 4, 7)),
        ("A NCD 19AG27 FVRS1LAC", date(2027, 8, 19)),
        ("A NCD 05MY27 FVRS1LAC", date(2027, 5, 5)),
    ],
)
def test_month_code_mapping(text, expected):
    assert parse_descriptor(text).maturity_date == expected


def test_spelled_month_abbreviations_parse():
    assert parse_descriptor("A NCD 29OCT27 FVRS1LAC").maturity_date == date(2027, 10, 29)
    assert parse_descriptor("A 12.75 NCD 26SEP27 FVRS10000").maturity_date == date(2027, 9, 26)
    assert parse_descriptor("T PTC 07MAY2026 SERIES").maturity_date == date(2026, 5, 7)


# --- Edge cases ---

def test_unknown_month_code_gives_no_date():
    d = parse_descriptor("ABC 9 NCD 16ZZ31 FVRS1LAC")
    assert d.maturity_date is None
    assert "unknown month code ZZ" in d.warnings
    assert d.coupon_pct == 9.0


def test_invalid_day_for_month_gives_no_date():
    d = parse_descriptor("ABC 9 NCD 31FB27 FVRS1LAC")
    assert d.maturity_date is None
    assert "invalid date 31FB27" in d.warnings


def test_thirty_first_of_june_is_invalid_but_thirty_first_of_jn_is_january():
    assert parse_descriptor("A NCD 31JU29 FVRS1LAC").maturity_date is None
    assert parse_descriptor("A NCD 31JN29 FVRS1LAC").maturity_date == date(2029, 1, 31)


def test_missing_face_value():
    d = parse_descriptor("ABC 9 NCD 28FB27")
    assert d.face_value is None
    assert "missing face value" in d.warnings
    assert d.coupon_pct == 9.0 and d.maturity_date == date(2027, 2, 28)


def test_face_value_without_amount():
    d = parse_descriptor("ABC 9 NCD 28FB27 FVRS")
    assert d.face_value is None
    assert "face value token without amount" in d.warnings


@pytest.mark.parametrize(
    "token,expected",
    [
        ("FVRS1LAC", 100000.0),
        ("FVRS 1LAC", 100000.0),
        ("FVRS10LAC", 1000000.0),
        ("FVRS12.5LAC", 1250000.0),
        ("FVRS1CR", 10000000.0),
        ("FVRS10000", 10000.0),
        ("FVRS1000", 1000.0),
        ("FVRS75000.01", 75000.01),
    ],
)
def test_face_value_units(token, expected):
    assert parse_descriptor(f"ABC 9 NCD 28FB27 {token}").face_value == pytest.approx(expected)


def test_ambiguous_coupon_returns_none():
    d = parse_descriptor("ABC 8.5 SR 9.1 NCD 01JN28 FVRS1LAC")
    assert d.coupon_pct is None
    assert any(w.startswith("ambiguous coupon candidates") or "series number" in w for w in d.warnings)
    assert d.maturity_date == date(2028, 1, 1)  # other fields are still reported


def test_coupon_not_immediately_before_kind_is_none():
    d = parse_descriptor("MIDLAND MICROFIN LIMITED NCD 28AP27 FVRS10000")
    assert d.coupon_pct is None
    assert "coupon not immediately before kind token" in d.warnings


def test_percent_suffix_on_coupon():
    assert parse_descriptor("UNIGOLD FINANCE LIMITED 11% NCD 06AG27 FVRS10000").coupon_pct == 11.0


def test_implausible_coupon_is_rejected():
    d = parse_descriptor("INDIAN RAILWAY FINANCE CORPORATION LIMITED SR 192 BD 01DC35 FVRS1LAC")
    assert d.coupon_pct is None
    assert any("implausible coupon 192" == w or "series number" in w for w in d.warnings)


def test_loa_upto_date_is_not_the_maturity():
    d = parse_descriptor("POWER FINANCE CORPORATION LIMITED 8.85 NCD 15JU30 FVRS10LAC LOA UPTO 03AG10")
    assert d.maturity_date == date(2030, 6, 15)
    d2 = parse_descriptor("MAHINDRA SR-II 8 NCD 24JL27 FVRS1000 LOAUPTO25JL17")
    assert d2.maturity_date == date(2027, 7, 24)


def test_malformed_maturity_token_is_reported():
    d = parse_descriptor("MUTHOOT FINANCE LIMITED 8.70 NCD 16JN236 FVRS1LAC")
    assert d.maturity_date is None
    assert "malformed date token 16JN236" in d.warnings


def test_two_different_maturity_dates_are_ambiguous():
    d = parse_descriptor("ABC 9 NCD 28FB27 FVRS1LAC NCD 30DC28")
    assert d.maturity_date is None
    assert "ambiguous maturity dates" in d.warnings


def test_repeated_identical_date_is_not_ambiguous():
    d = parse_descriptor("INDIA TRUST SERIES A3 PTC 25SEP24 SERIES A3 PTC 25SEP24")
    assert d.maturity_date == date(2024, 9, 25)


def test_perpetual_hint_from_token():
    d = parse_descriptor("STATE BANK OF INDIA SR I 7.75 BD PERPETUAL FVRS1CR")
    assert d.perpetual_hint is True
    assert d.at1_hint is False
    assert d.maturity_date is None
    assert d.coupon_pct == 7.75


def test_at1_and_call_hints_are_token_based():
    assert parse_descriptor("BANK AT1 9 NCD 01DC28 FVRS1LAC").at1_hint is True
    assert parse_descriptor("BANK 9 NCD 01DC28 FVRS1LAC CALL").call_hint is True
    # 'AL1' in trust names is not AT1
    assert parse_descriptor("INDIA UNIVERSAL TRUST AL1 SERIES A3 PTC 25SEP24").at1_hint is False


@pytest.mark.parametrize("text", ["", "   ", "\r", "\r\n", "\t"])
def test_empty_or_whitespace(text):
    d = parse_descriptor(text)
    assert d.raw == ""
    assert d.coupon_pct is None and d.maturity_date is None and d.face_value is None
    assert d.warnings == ["empty descriptor"]


@pytest.mark.parametrize("value", [None, 123, b"ABC 9 NCD 28FB27 FVRS1LAC"])
def test_non_string_input_does_not_raise(value):
    d = parse_descriptor(value)
    assert isinstance(d, Descriptor)
    assert d.warnings == ["not a string"]


def test_trailing_whitespace_and_cr_are_ignored():
    base = parse_descriptor("PROGFIN PRIVATE LIMITED 11 NCD 13JN27 FVRS10000")
    assert parse_descriptor("PROGFIN PRIVATE LIMITED 11 NCD 13JN27 FVRS10000\r") == base
    assert parse_descriptor("  PROGFIN PRIVATE LIMITED 11 NCD 13JN27 FVRS10000 \t\n") == base


def test_plain_issuer_name_has_no_descriptor_fields():
    d = parse_descriptor("Sikkim State")
    assert d.issuer == "SIKKIM STATE"
    assert d.coupon_pct is None and d.maturity_date is None and d.face_value is None
    assert d.instrument_kind is None and d.series is None


def test_result_is_frozen():
    d = parse_descriptor("ABC 9 NCD 28FB27 FVRS1LAC")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.coupon_pct = 1.0  # type: ignore[misc]


def test_never_raises_on_random_input():
    rng = random.Random(20260501)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .-%\r\t()"
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        d = parse_descriptor(text)
        assert isinstance(d, Descriptor)
        if d.maturity_date is not None:
            assert isinstance(d.maturity_date, date)


# --- Corpus fixture (names copied from the DB; expectations hand-derived) ---

def _load_fixture():
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        descriptor, expected = line.split("\t")
        rows.append((descriptor, expected))
    return rows


FIXTURE_ROWS = _load_fixture()
DATE_TOKEN = re.compile(r"\d{1,2}[A-Z]{2,4}\d{2,4}")


def _parse_expected(expected: str) -> dict:
    out = {}
    for part in expected.split(";"):
        key, value = part.split("=", 1)
        out[key] = value
    return out


def _as_value(key, text):
    if text == "None":
        return None
    if key == "coupon":
        return float(text)
    if key == "fv":
        return float(text)
    if key == "perp":
        return text == "True"
    return text


KEYMAP = {
    "issuer": "issuer",
    "series": "series",
    "coupon": "coupon_pct",
    "kind": "instrument_kind",
    "mat": "maturity_date",
    "fv": "face_value",
    "perp": "perpetual_hint",
}


def test_fixture_is_populated():
    assert len(FIXTURE_ROWS) >= 80
    fails = [r for r in FIXTURE_ROWS if r[1] == "FAIL"]
    assert len(fails) >= 10


@pytest.mark.parametrize("descriptor,expected", FIXTURE_ROWS, ids=[r[0][:40] for r in FIXTURE_ROWS])
def test_corpus_fixture(descriptor, expected):
    d = parse_descriptor(descriptor)
    if expected == "FAIL":
        assert d.coupon_pct is None
        assert d.maturity_date is None
        assert d.face_value is None
        return
    for key, text in _parse_expected(expected).items():
        field = KEYMAP[key]
        want = _as_value(key, text)
        got = getattr(d, field)
        if key == "mat":
            assert got == (date.fromisoformat(text) if text != "None" else None), descriptor
        elif key in ("coupon", "fv"):
            if want is None:
                assert got is None, (descriptor, field)
            else:
                assert got == pytest.approx(want), (descriptor, field)
        else:
            assert got == want, (descriptor, field)


@pytest.mark.parametrize("descriptor,expected", FIXTURE_ROWS, ids=[r[0][:40] for r in FIXTURE_ROWS])
def test_corpus_no_silent_date_drop(descriptor, expected):
    # If a date-like token is present but no maturity is returned, a warning must say why.
    d = parse_descriptor(descriptor)
    if DATE_TOKEN.search(descriptor.upper()) and d.maturity_date is None:
        assert d.warnings, descriptor


def test_corpus_parsed_dates_are_real_calendar_dates():
    for descriptor, _ in FIXTURE_ROWS:
        d = parse_descriptor(descriptor)
        if d.maturity_date is not None:
            assert date(d.maturity_date.year, d.maturity_date.month, d.maturity_date.day) == d.maturity_date


def test_series_number_before_kind_is_not_a_coupon():
    from india_bonds.enrichment.descriptor import parse_descriptor
    d = parse_descriptor("ZETA FIN SR 2 BD 31OT31")
    assert d.coupon_pct is None
    assert any("series number" in w for w in d.warnings)
    # a real coupon directly after a series is unaffected
    assert parse_descriptor("REC LIMITED SR 261 8.3 BD 31OT31 FVRS1LAC").coupon_pct == 8.3
