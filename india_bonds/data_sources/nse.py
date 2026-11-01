"""NSE connectors: active debt universe and CM bhavcopy (debt series only).

Heuristics (documented, see SPEC "verified data-source facts"):
- Universe: rows kept only when Instrument Type == 'DEBT' (configurable).
  corporate_only=True (default) additionally drops government securities by
  ISIN prefix 'IN00' (sovereign issues such as Sovereign Gold Bonds carry
  IN00xxxxxxxx ISINs; corporate ISINs start 'INE'). This is a heuristic, not a
  verified classification.
- Bhavcopy debt series: by default a series is treated as corporate debt when
  its code matches ^N[0-9A-Z]$ (N0..N9, NA..NZ). Verified on the 2026-10-30
  sample: every such series held corporate NCD / tax-free bond rows; GB, GS,
  TB, SG and equity series (EQ, BE, BZ, SM, ST, BL, BT, ...) do not match.
  Pass series_include= to use an explicit set instead.
- coupon_hint is regex-extracted from the instrument name (e.g. 'ECL 8.88%
  2028' -> 8.88). It is a hint only and must never be stored as verified coupon.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

from .base import STATUS_ERROR, STATUS_OK, STATUS_UNAVAILABLE, Connector, SourceRecord

URL_UNIVERSE = "https://nsearchives.nseindia.com/content/equities/List_of_Active_Securities_CM_DEBT.csv"
URL_BHAV_TEMPLATE = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"

ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")
GOV_ISIN_PREFIX = "IN00"
# Corporate debt ISINs start "INE". Everything else in the active-debt list is not a corporate bond:
# IN00/IN0x sovereign, IN1x-IN4x state development loans (e.g. "Karnataka State"), INS social-stock
# instruments. corporate_only keeps INE only (verified on the live list: 1,764 INE vs 3,580 state-loan ISINs).
CORPORATE_ISIN_PREFIX = "INE"
CORPORATE_SERIES_RE = re.compile(r"^N[0-9A-Z]$")
COUPON_HINT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,4})?)\s*%")


def is_corporate_debt_series(code: str) -> bool:
    return bool(CORPORATE_SERIES_RE.match(code or ""))


def csv_rows_from_bytes(body: bytes) -> list[dict]:
    """Decode CSV bytes (BOM tolerant) into DictReader rows."""
    text = body.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def _clean_row(raw: Mapping) -> dict:
    return {str(k or "").strip(): str(v or "").strip() for k, v in raw.items()}


def _num(value: str) -> Optional[float]:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class NseDebtUniverse(Connector):
    """Active CM debt securities list (ISIN, name, instrument type). No coupon/maturity."""

    source = "nse_debt_universe"
    kind = "csv"
    url = URL_UNIVERSE
    filename = "List_of_Active_Securities_CM_DEBT.csv"

    def __init__(self, instrument_types: Iterable[str] = ("DEBT",),
                 corporate_only: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.instrument_types = frozenset(instrument_types)
        self.corporate_only = corporate_only
        self.stats: dict = {}

    def parse(self, rows: Iterable[Mapping]) -> list[dict]:
        """rows: iterable of dicts keyed by the NSE header (csv.DictReader output)."""
        stats = {"rows": 0, "type_filtered": 0, "invalid_isin": 0, "gov_dropped": 0, "kept": 0}
        out: list[dict] = []
        for raw in rows:
            stats["rows"] += 1
            row = _clean_row(raw)
            if row.get("Instrument Type") not in self.instrument_types:
                stats["type_filtered"] += 1
                continue
            isin = row.get("ISIN", "")
            if not ISIN_RE.match(isin):
                stats["invalid_isin"] += 1
                continue
            if self.corporate_only and not isin.startswith(CORPORATE_ISIN_PREFIX):
                stats["gov_dropped"] += 1
                continue
            stats["kept"] += 1
            out.append({
                "isin": isin,
                "name": row.get("Company Name", ""),
                "instrument_type": row.get("Instrument Type", ""),
            })
        self.stats = stats
        return out

    def parse_file(self, path: Path | str) -> list[dict]:
        return self.parse(csv_rows_from_bytes(Path(path).read_bytes()))


def bhav_url(trade_date: date) -> str:
    return URL_BHAV_TEMPLATE.format(ymd=trade_date.strftime("%Y%m%d"))


class NseCmBhavcopy(Connector):
    """NSE CM bhavcopy for one trade date, filtered to debt series.

    fetch() tries trade_date, then up to ``walkback_days`` earlier calendar
    days, stopping at the first valid file (weekends/holidays 404 or return a
    shell). A network failure stops the walk-back immediately.
    """

    source = "nse_cm_bhavcopy"
    kind = "zip"
    min_bytes = 500

    def __init__(self, trade_date: date, series_include: Optional[Iterable[str]] = None,
                 walkback_days: int = 5, **kwargs) -> None:
        super().__init__(**kwargs)
        self.trade_date = trade_date
        self.series_include = frozenset(series_include) if series_include is not None else None
        self.walkback_days = walkback_days
        self.resolved_date: Optional[date] = None
        self.stats: dict = {}

    def _series_ok(self, code: str) -> bool:
        if self.series_include is None:
            return is_corporate_debt_series(code)
        return code in self.series_include

    def fetch(self) -> SourceRecord:
        last: Optional[SourceRecord] = None
        for offset in range(self.walkback_days + 1):
            d = self.trade_date - timedelta(days=offset)
            name = f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
            record, http_status = self._retrieve(bhav_url(d), name)
            if record.status == STATUS_OK:
                self.resolved_date = d
                note = f"trade_date={d.isoformat()}"
                if offset:
                    note += f"; walked back {offset} day(s) from {self.trade_date.isoformat()}"
                return replace(record, note=note)
            last = record
            if http_status is None:  # transport failure: do not keep hammering
                return record
        assert last is not None
        return replace(last, note=f"no bhavcopy file for {self.trade_date.isoformat()} "
                                  f"or previous {self.walkback_days} days; last: {last.note}")

    def parse_rows(self, rows: Iterable[Mapping]) -> list[dict]:
        stats = {"rows": 0, "series_filtered": 0, "invalid_isin": 0, "kept": 0}
        out: list[dict] = []
        required = {"ISIN", "SctySrs", "FinInstrmNm", "ClsPric", "TtlTradgVol"}
        first = True
        for raw in rows:
            row = _clean_row(raw)
            if first:
                missing = required - set(row)
                if missing:
                    raise ValueError(f"bhavcopy header missing columns: {sorted(missing)}")
                first = False
            stats["rows"] += 1
            series = row.get("SctySrs", "")
            if not self._series_ok(series):
                stats["series_filtered"] += 1
                continue
            isin = row.get("ISIN", "")
            if not ISIN_RE.match(isin):
                stats["invalid_isin"] += 1
                continue
            name = row.get("FinInstrmNm", "")
            hint = COUPON_HINT_RE.search(name)
            trades = _num(row.get("TtlNbOfTxsExctd", ""))
            stats["kept"] += 1
            out.append({
                "isin": isin,
                "symbol": row.get("TckrSymb", ""),
                "series": series,
                "name": name,
                "trade_date": row.get("TradDt") or None,
                "close": _num(row.get("ClsPric", "")),
                "last": _num(row.get("LastPric", "")),
                "prev_close": _num(row.get("PrvsClsgPric", "")),
                "volume": _num(row.get("TtlTradgVol", "")),
                "value": _num(row.get("TtlTrfVal", "")),
                "trades": int(trades) if trades is not None else None,
                "coupon_hint": float(hint.group(1)) if hint else None,
            })
        self.stats = stats
        return out

    def parse_zip_bytes(self, body: bytes) -> list[dict]:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError(f"expected exactly one CSV in bhavcopy zip, found {len(members)}")
            text = zf.read(members[0]).decode("utf-8-sig", errors="replace")
        return self.parse_rows(csv.DictReader(io.StringIO(text)))

    def parse_file(self, path: Path | str) -> list[dict]:
        return self.parse_zip_bytes(Path(path).read_bytes())


# ---------------------------------------------------------------- NSE OTC/RFQ corporate bond trades
#
# Source: NSE 'liveCorp-bonds' JSON API (not the archive). Verified shape: {"data": [...],
# "timestamp": "30-Oct-2026 17:13", ...}. Each row: descriptor, isin, ltp, lty, noOfTrades,
# tradeValue, wap, way. Prices are reported PER 100 FACE. Clean-price basis and the unit
# of tradeValue are UNVERIFIED: never state either as fact. Data are for personal/internal
# research use only and are not redistributed.

CORP_TRADES_URL = "https://www.nseindia.com/api/liveCorp-bonds"
CORP_TRADES_INDICES = ("otctrades_listed", "rfqtrades_listed")
CORP_TRADES_MARKET_TYPE = "CBM"
CORP_TRADES_MAX_AGE_DAYS = 4
CORP_TIMESTAMP_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M")
CORP_TRADES_BASIS_LABEL = "as reported by NSE; clean-price basis and trade-value unit unverified"


def corp_trades_url(index: str) -> str:
    if index not in CORP_TRADES_INDICES:
        raise ValueError(f"unknown corp-trades index {index!r}")
    return f"{CORP_TRADES_URL}?index={index}&marketType={CORP_TRADES_MARKET_TYPE}"


def index_from_url(url: str | None) -> str | None:
    """Index name from a corp-trades URL, or None."""
    if not url:
        return None
    values = parse_qs(urlsplit(url).query).get("index")
    return values[0] if values else None


def _finite_float(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_corp_timestamp(text) -> tuple[str, str]:
    """'30-Oct-2026 17:13[:00]' -> (naive ISO 'YYYY-MM-DDTHH:MM:SS', 'YYYY-MM-DD').

    The time zone of NSE's timestamp is not verified; it is stored without one.
    Raises ValueError when the text is not in a recognised format.
    """
    raw = str(text or "").strip()
    for fmt in CORP_TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return dt.strftime("%Y-%m-%dT%H:%M:%S"), dt.date().isoformat()
    raise ValueError(f"unrecognised NSE corp-trades timestamp {raw!r}")


def select_one_per_isin(rows: Iterable[Mapping]) -> tuple[list[dict], int, int]:
    """Choose one row per ISIN for ingestion.

    Only rows with a usable ltp and trade_date are candidates. Among candidates for one ISIN
    the row with more trades wins (tie: larger trade_value; further ties keep the first seen,
    i.e. otctrades_listed before rfqtrades_listed). Returns
    (selected rows, dropped duplicate rows, ISINs with no usable row).
    """
    best: dict[str, dict] = {}
    dropped = 0
    no_price: set[str] = set()
    for row in rows:
        isin = row["isin"]
        if row.get("ltp") is None or not row.get("trade_date"):
            no_price.add(isin)
            continue
        if isin not in best:
            best[isin] = dict(row)
            continue
        dropped += 1
        key = (row.get("trades") or 0, row.get("trade_value") or 0.0)
        cur = best[isin]
        if key > ((cur.get("trades") or 0), (cur.get("trade_value") or 0.0)):
            best[isin] = dict(row)
    selected = [best[i] for i in sorted(best)]
    return selected, dropped, len(no_price - set(best))


class NseCorpBondTrades(Connector):
    """NSE OTC/RFQ traded bonds for listed corporate debt: one JSON per index.

    fetch() returns a list with one SourceRecord per index (never raises). Each response must
    be application/json with a 'data' list; anything else is 'error' (e.g. an HTML shell).
    Requests run sequentially through the base throttle (>= 1 s apart) with one retry on
    transport failure, 429 or 5xx. Raw JSON is saved under data/raw/nse_corp_trades/.
    """

    source = "nse_corp_trades"
    kind = "json"
    url = None  # two URLs, one per index; see fetch()

    def __init__(self, indices: Iterable[str] = CORP_TRADES_INDICES, **kwargs) -> None:
        super().__init__(**kwargs)
        self.indices = tuple(indices)
        self.stats: dict = {}

    # ----- network
    def fetch(self) -> list[SourceRecord]:  # type: ignore[override]
        return [self._fetch_index(idx) for idx in self.indices]

    def _fetch_index(self, index: str) -> SourceRecord:
        url = corp_trades_url(index)
        try:
            resp, err = self._get(url)
            if resp is None:
                return self._record(url, STATUS_UNAVAILABLE, err)
            ct = resp.headers.get("Content-Type") if resp.headers else None
            body = resp.content or b""
            if resp.status_code != 200:
                return self._record(url, STATUS_UNAVAILABLE, f"HTTP {resp.status_code}", ct, body)
            mime = (ct or "").split(";")[0].strip().lower()
            if mime != "application/json":
                return self._record(
                    url, STATUS_ERROR, f"content-type '{mime or 'missing'}' is not application/json", ct, body)
            try:
                payload = json.loads(body.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as exc:
                return self._record(url, STATUS_ERROR, f"invalid JSON: {exc}", ct, body)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                return self._record(url, STATUS_ERROR, "JSON has no 'data' list", ct, body)
            path = self._save(f"{index}.json", body)
            return self._record(url, STATUS_OK, f"index={index}", ct, body, path)
        except Exception as exc:  # noqa: BLE001 - connector must not raise
            return self._record(url, STATUS_ERROR, f"unexpected error: {type(exc).__name__}: {exc}")

    # ----- parsing
    def parse_file(self, path: Path | str, index: str) -> list[dict]:
        """Parse one saved raw JSON for ``index``. Raises ValueError on a malformed payload."""
        try:
            payload = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid corp-trades JSON: {exc}") from exc
        return self.parse(payload, index)

    def parse_record(self, rec: SourceRecord) -> list[dict]:
        """Parse an ok SourceRecord (index taken from its URL)."""
        index = index_from_url(rec.url)
        if index is None or rec.raw_path is None:
            raise ValueError("corp-trades record has no index or raw path")
        return self.parse_file(rec.raw_path, index)

    def parse(self, payload: Mapping, index: str) -> list[dict]:
        """Rows: isin, name, ltp, wap, lty, way, trade_value, trades, index, response_timestamp,
        trade_date, source_url. Invalid ISINs and IN00 sovereign ISINs are dropped (counted in
        self.stats). Raises ValueError when the response timestamp is missing or unparseable."""
        response_timestamp, trade_date = parse_corp_timestamp(payload.get("timestamp"))
        stats = {"rows": 0, "invalid_isin": 0, "gov_dropped": 0, "kept": 0}
        out: list[dict] = []
        url = corp_trades_url(index)
        for raw in payload.get("data") or []:
            stats["rows"] += 1
            if not isinstance(raw, Mapping):
                stats["invalid_isin"] += 1
                continue
            isin = str(raw.get("isin") or "").strip()
            if not ISIN_RE.match(isin):
                stats["invalid_isin"] += 1
                continue
            if isin.startswith(GOV_ISIN_PREFIX):
                stats["gov_dropped"] += 1
                continue
            trades = _finite_float(raw.get("noOfTrades"))
            stats["kept"] += 1
            out.append({
                "isin": isin,
                "name": str(raw.get("descriptor") or "").strip(),
                "ltp": _finite_float(raw.get("ltp")),
                "wap": _finite_float(raw.get("wap")),
                "lty": _finite_float(raw.get("lty")),
                "way": _finite_float(raw.get("way")),
                "trade_value": _finite_float(raw.get("tradeValue")),
                "trades": int(trades) if trades is not None else None,
                "index": index,
                "response_timestamp": response_timestamp,
                "trade_date": trade_date,
                "source_url": url,
            })
        self.stats = stats
        return out
