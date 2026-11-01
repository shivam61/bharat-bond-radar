"""Connector tests. HTTP is mocked by replacing session.get; no network access."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
import requests

from india_bonds.data_sources.base import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    USER_AGENT,
    Connector,
    validate_payload,
)
from india_bonds.data_sources.bse import BSE_NOTE, BseDebt
from india_bonds.data_sources.nse import (
    URL_UNIVERSE,
    NseCmBhavcopy,
    NseDebtUniverse,
    bhav_url,
    csv_rows_from_bytes,
    is_corporate_debt_series,
)

FIXTURES = Path(__file__).parent / "fixtures"
UNIVERSE_CSV = (FIXTURES / "nse_active_debt_excerpt.csv").read_bytes()
BHAV_CSV = (FIXTURES / "nse_bhav_excerpt.csv").read_bytes()
BHAV_MEMBER = "BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv"
HTML_SHELL = (b"<!DOCTYPE html><html><head><title>NSE</title></head><body>"
              + b"<p>shell</p>" * 200 + b"</body></html>")


def make_zip(csv_bytes: bytes, member: str = BHAV_MEMBER) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, csv_bytes)
    return buf.getvalue()


class FakeResp:
    def __init__(self, status: int = 200, content: bytes = b"", ctype: str | None = "text/csv"):
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": ctype} if ctype else {}


class FakeSession:
    """handler(url) -> FakeResp or raises. Records every requested URL."""

    def __init__(self, handler):
        self.handler = handler
        self.urls: list[str] = []
        self.headers: dict = {}

    def get(self, url, timeout=None):
        self.urls.append(url)
        result = self.handler(url)
        if isinstance(result, BaseException):
            raise result
        return result


def no_sleep(_seconds):
    return None


def make_connector(cls, handler, tmp_path, **kwargs):
    session = FakeSession(handler)
    return cls(session=session, raw_root=tmp_path / "raw", sleep=no_sleep, **kwargs), session


# ---------------------------------------------------------------- base.py

def test_valid_csv_saved_with_record(tmp_path):
    conn, session = make_connector(
        NseDebtUniverse, lambda url: FakeResp(200, UNIVERSE_CSV, "text/csv; charset=utf-8"), tmp_path)
    rec = conn.fetch()
    assert rec.status == STATUS_OK
    assert rec.url == URL_UNIVERSE
    assert rec.size_bytes == len(UNIVERSE_CSV)
    assert rec.sha256 and len(rec.sha256) == 64
    assert rec.fetched_at.endswith("+00:00")
    assert rec.raw_path is not None
    saved = Path(rec.raw_path)
    assert saved.parent == tmp_path / "raw" / "nse_debt_universe"
    assert saved.read_bytes() == UNIVERSE_CSV
    assert saved.name.endswith("_List_of_Active_Securities_CM_DEBT.csv")


def test_html_shell_with_200_rejected_and_not_saved(tmp_path):
    conn, _ = make_connector(
        NseDebtUniverse, lambda url: FakeResp(200, HTML_SHELL, "text/html; charset=UTF-8"), tmp_path)
    rec = conn.fetch()
    assert rec.status == STATUS_ERROR
    assert rec.raw_path is None
    assert "html" in rec.note.lower()
    assert not (tmp_path / "raw").exists() or not any((tmp_path / "raw").rglob("*"))


def test_html_content_with_csv_header_still_rejected(tmp_path):
    # Server lies about type: body sniffing must still catch the shell.
    conn, _ = make_connector(
        NseDebtUniverse, lambda url: FakeResp(200, HTML_SHELL, "text/csv"), tmp_path)
    rec = conn.fetch()
    assert rec.status == STATUS_ERROR
    assert rec.raw_path is None


def test_missing_content_type_rejected(tmp_path):
    conn, _ = make_connector(
        NseDebtUniverse, lambda url: FakeResp(200, UNIVERSE_CSV, None), tmp_path)
    assert conn.fetch().status == STATUS_ERROR


def test_too_small_payload_unavailable(tmp_path):
    conn, _ = make_connector(
        NseDebtUniverse, lambda url: FakeResp(200, b"a,b\n1,2\n", "text/csv"), tmp_path)
    rec = conn.fetch()
    assert rec.status == STATUS_UNAVAILABLE
    assert rec.raw_path is None


def test_non_200_unavailable(tmp_path):
    conn, _ = make_connector(NseDebtUniverse, lambda url: FakeResp(403, b"", "text/html"), tmp_path)
    rec = conn.fetch()
    assert rec.status == STATUS_UNAVAILABLE
    assert rec.note == "HTTP 403"


def test_network_exception_is_unavailable_not_raised(tmp_path):
    conn, session = make_connector(
        NseDebtUniverse, lambda url: ConnectionError("boom"), tmp_path)
    rec = conn.fetch()  # must not raise
    assert rec.status == STATUS_UNAVAILABLE
    assert "boom" in rec.note
    assert len(session.urls) == 2  # one retry max
    assert rec.raw_path is None


def test_retry_once_then_success(tmp_path):
    calls = {"n": 0}

    def handler(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return requests.exceptions.Timeout("slow")
        return FakeResp(200, UNIVERSE_CSV, "text/csv")

    conn, session = make_connector(NseDebtUniverse, handler, tmp_path)
    assert conn.fetch().status == STATUS_OK
    assert len(session.urls) == 2


def test_server_error_retried_then_unavailable(tmp_path):
    conn, session = make_connector(NseDebtUniverse, lambda url: FakeResp(503, b"", "text/html"), tmp_path)
    rec = conn.fetch()
    assert rec.status == STATUS_UNAVAILABLE
    assert len(session.urls) == 2


def test_throttle_waits_between_requests(tmp_path):
    waits: list[float] = []
    session = FakeSession(lambda url: FakeResp(404, b"", "text/html"))
    conn = NseDebtUniverse(session=session, raw_root=tmp_path, sleep=waits.append,
                           clock=lambda: 100.0)
    conn.fetch()
    conn.fetch()
    # clock frozen at 100.0 -> second request must wait the full 1 s interval
    assert waits == [pytest.approx(1.0)]


def test_default_session_carries_user_agent():
    conn = NseDebtUniverse(raw_root=Path("/nonexistent-raw"))
    assert conn.session.headers["User-Agent"] == USER_AGENT == "Mozilla/5.0"
    assert conn.timeout_s == 20
    assert conn.min_interval_s >= 1.0
    assert conn.max_attempts == 2


def test_validate_payload_directly():
    assert validate_payload("csv", "text/csv", UNIVERSE_CSV, 500) == (None, None)
    status, reason = validate_payload("csv", "text/html", UNIVERSE_CSV, 500)
    assert status == STATUS_ERROR and "html" in reason.lower()
    status, _ = validate_payload("zip", "application/zip", b"not a zip at all" * 100, 500)
    assert status == STATUS_ERROR


# ---------------------------------------------------------------- nse universe

def test_universe_parse_debt_only_gov_dropped_invalid_skipped():
    conn = NseDebtUniverse()
    rows = csv_rows_from_bytes(UNIVERSE_CSV)
    rows.append({"Sr.No.": "900", "Company Name": "Bad ISIN Ltd", "ISIN": "INE12", "Instrument Type": "DEBT"})
    rows.append({"Sr.No.": "901", "Company Name": "Lower Case", "ISIN": "ine528s07110", "Instrument Type": "DEBT"})
    out = conn.parse(rows)
    isins = [r["isin"] for r in out]
    assert all(len(i) == 12 and i.startswith("IN") for i in isins)
    assert not any(i.startswith("IN00") for i in isins)
    assert "INE12" not in isins and "ine528s07110" not in isins
    assert all(r["instrument_type"] == "DEBT" for r in out)
    assert set(out[0]) == {"isin", "name", "instrument_type"}
    assert conn.stats["invalid_isin"] == 2
    assert conn.stats["gov_dropped"] == 3
    assert conn.stats["type_filtered"] == 5  # 5 Hybrid head rows
    assert len(out) == conn.stats["kept"] == 17


def test_universe_include_gov_and_other_types():
    rows = csv_rows_from_bytes(UNIVERSE_CSV)
    all_types = NseDebtUniverse(instrument_types=("DEBT", "Hybrid", "Equity"), corporate_only=False)
    out = all_types.parse(rows)
    assert len(out) == len(rows)
    assert any(r["isin"].startswith("IN00") for r in out)
    assert any(r["instrument_type"] == "Hybrid" for r in out)


def test_universe_url_constant():
    assert URL_UNIVERSE == ("https://nsearchives.nseindia.com/content/equities/"
                            "List_of_Active_Securities_CM_DEBT.csv")


# ---------------------------------------------------------------- nse bhavcopy

def test_bhav_url_pattern():
    assert bhav_url(date(2026, 10, 30)) == (
        "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv.zip")


def test_bhav_zip_parse_debt_series_and_fields(tmp_path):
    conn = NseCmBhavcopy(date(2026, 10, 30))
    out = conn.parse_zip_bytes(make_zip(BHAV_CSV))
    assert {r["series"] for r in out} <= {"N0", "N1"}
    assert len(out) == 24 == conn.stats["kept"]
    assert conn.stats["series_filtered"] == 6  # 3 GS + 3 EQ
    ecl = next(r for r in out if r["isin"] == "INE528S07110")
    assert ecl["name"] == "ECL 8.88% 2028 SR V"
    assert ecl["series"] == "N1"
    assert ecl["close"] == pytest.approx(990.00)
    assert ecl["volume"] == 26
    assert ecl["trades"] == 3
    assert ecl["trade_date"] == "2026-10-30"
    assert ecl["coupon_hint"] == pytest.approx(8.88)
    assert ecl["symbol"] == "888ECL28"


def test_bhav_coupon_hint_is_hint_only_none_when_absent():
    conn = NseCmBhavcopy(date(2026, 10, 30))
    rows = [{"ISIN": "INE00XL07015", "SctySrs": "N0", "FinInstrmNm": "BNAL SR TR 1",
             "ClsPric": "9820.00", "TtlTradgVol": "16", "TckrSymb": "x", "TradDt": "2026-10-30",
             "PrvsClsgPric": "", "LastPric": "", "TtlNbOfTxsExctd": ""}]
    out = conn.parse_rows(rows)
    assert out[0]["coupon_hint"] is None
    assert out[0]["prev_close"] is None and out[0]["trades"] is None


def test_bhav_custom_series_include_set():
    conn = NseCmBhavcopy(date(2026, 10, 30), series_include={"N1"})
    out = conn.parse_zip_bytes(make_zip(BHAV_CSV))
    assert out and {r["series"] for r in out} == {"N1"}
    assert len(out) == 10


def test_bhav_invalid_isin_skipped():
    conn = NseCmBhavcopy(date(2026, 10, 30))
    rows = [
        {"ISIN": "INE12", "SctySrs": "N0", "FinInstrmNm": "bad", "ClsPric": "1", "TtlTradgVol": "1"},
        {"ISIN": "", "SctySrs": "N0", "FinInstrmNm": "blank", "ClsPric": "1", "TtlTradgVol": "1"},
        {"ISIN": "INE528S07110", "SctySrs": "N1", "FinInstrmNm": "ok", "ClsPric": "990", "TtlTradgVol": "26"},
    ]
    out = conn.parse_rows(rows)
    assert [r["isin"] for r in out] == ["INE528S07110"]
    assert conn.stats["invalid_isin"] == 2


def test_bhav_missing_columns_raises_value_error():
    conn = NseCmBhavcopy(date(2026, 10, 30))
    with pytest.raises(ValueError):
        conn.parse_rows([{"ISIN": "INE528S07110"}])


def test_series_heuristic():
    for code in ("N0", "N1", "N9", "NA", "NC", "NE", "NZ"):
        assert is_corporate_debt_series(code)
    for code in ("GB", "GS", "TB", "SG", "EQ", "BE", "BZ", "SM", "ST", "BL", "BT", "N", "N10", ""):
        assert not is_corporate_debt_series(code)


def test_bhav_walkback_over_404(tmp_path):
    target = date(2026, 10, 30)  # Friday
    good_day = date(2026, 10, 27)
    seen = []

    def handler(url):
        seen.append(url)
        if "20261027" in url:
            return FakeResp(200, make_zip(BHAV_CSV), "application/zip")
        return FakeResp(404, b"", "text/html")

    session = FakeSession(handler)
    conn = NseCmBhavcopy(target, session=session, raw_root=tmp_path, sleep=no_sleep)
    rec = conn.fetch()
    assert rec.status == STATUS_OK
    assert conn.resolved_date == good_day
    assert "trade_date=2026-10-27" in rec.note
    assert len(session.urls) == 4  # 30, 29, 28, 27
    assert Path(rec.raw_path).read_bytes()[:2] == b"PK"
    assert rec.url.endswith("20261027_F_0000.csv.zip")
    parsed = conn.parse_file(rec.raw_path)
    assert len(parsed) == 24


def test_bhav_walkback_html_shell_then_gives_up(tmp_path):
    session = FakeSession(lambda url: FakeResp(200, HTML_SHELL, "text/html"))
    conn = NseCmBhavcopy(date(2026, 10, 30), session=session, raw_root=tmp_path, sleep=no_sleep)
    rec = conn.fetch()
    assert rec.status == STATUS_ERROR  # last attempt was an HTML shell
    assert rec.raw_path is None
    assert len(session.urls) == 6  # trade date + 5 earlier days
    assert "previous 5 days" in rec.note


def test_bhav_all_404_unavailable_after_six_dates(tmp_path):
    session = FakeSession(lambda url: FakeResp(404, b"", "text/html"))
    conn = NseCmBhavcopy(date(2026, 10, 30), session=session, raw_root=tmp_path, sleep=no_sleep)
    rec = conn.fetch()
    assert rec.status == STATUS_UNAVAILABLE
    assert len(session.urls) == 6
    assert conn.resolved_date is None


def test_bhav_network_failure_stops_walkback(tmp_path):
    session = FakeSession(lambda url: ConnectionError("dns down"))
    conn = NseCmBhavcopy(date(2026, 10, 30), session=session, raw_root=tmp_path, sleep=no_sleep)
    rec = conn.fetch()
    assert rec.status == STATUS_UNAVAILABLE
    assert "dns down" in rec.note
    assert len(session.urls) == 2  # only the first date, with its single retry


def test_bhav_zip_with_two_members_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", BHAV_CSV)
        zf.writestr("b.csv", BHAV_CSV)
    with pytest.raises(ValueError):
        NseCmBhavcopy(date(2026, 10, 30)).parse_zip_bytes(buf.getvalue())


# ---------------------------------------------------------------- bse

def test_bse_no_network_call_and_unavailable(tmp_path):
    session = FakeSession(lambda url: pytest.fail("BSE must not hit the network"))
    rec = BseDebt(session=session, raw_root=tmp_path).fetch()
    assert session.urls == []
    assert rec.status == STATUS_UNAVAILABLE
    assert rec.note == BSE_NOTE == "no verified BSE file URL; use CSV import"
    assert rec.url is None and rec.raw_path is None
    assert rec.fetched_at.endswith("+00:00")


def test_connector_base_fetch_requires_url(tmp_path):
    session = FakeSession(lambda url: FakeResp(200, UNIVERSE_CSV))
    with pytest.raises(NotImplementedError):
        Connector(session=session, raw_root=tmp_path).fetch()
