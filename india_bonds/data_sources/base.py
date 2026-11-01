"""Base connector: HTTP session, throttling, one retry, validation, raw persistence.

Contract (SPEC "Connector API"):
- ``Connector.fetch()`` returns a ``SourceRecord``; it never raises on network
  failure (status ``unavailable``).
- Raw bytes are written to ``<raw_root>/<source>/<UTC-ts>_<name>`` only when
  validation passes.
- Status meanings:
    ok          valid payload fetched and saved to raw_path
    unavailable no usable file: network failure, non-200 (e.g. 404), too small,
                or not published
    error       a response arrived but was the wrong kind (e.g. text/html shell
                returned with HTTP 200 for a bad path)
- This module does NOT touch the database. The caller persists the record.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# NSE archive edge drops requests whose UA has a custom suffix (verified: read timeout);
# plain "Mozilla/5.0" is served. Rate limiting (1 req/s) is still enforced in Connector.
USER_AGENT = "Mozilla/5.0"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data" / "raw"

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
CSV_TYPES = frozenset({
    "text/csv", "application/csv", "text/plain",
    "application/octet-stream", "application/vnd.ms-excel",
})
ZIP_TYPES = frozenset({
    "application/zip", "application/x-zip-compressed", "application/x-zip",
    "application/octet-stream",
})


@dataclass(frozen=True)
class SourceRecord:
    source: str
    url: Optional[str]
    fetched_at: str  # ISO-8601 UTC
    content_type: Optional[str]
    size_bytes: int
    sha256: Optional[str]
    raw_path: Optional[str]
    status: str  # ok | unavailable | error
    note: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_payload(kind: str, content_type: Optional[str], body: bytes,
                     min_bytes: int) -> Tuple[Optional[str], Optional[str]]:
    """Return (None, None) when valid, else (status, reason).

    kind is 'csv' or 'zip'. Status is 'unavailable' for size problems and
    'error' for wrong content.
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in HTML_TYPES:
        return STATUS_ERROR, f"unexpected content-type {ct} (HTML shell) where {kind} expected"
    allowed = CSV_TYPES if kind == "csv" else ZIP_TYPES
    if ct not in allowed:
        return STATUS_ERROR, f"content-type '{ct or 'missing'}' not accepted for {kind}"
    if len(body) < min_bytes:
        return STATUS_UNAVAILABLE, f"payload {len(body)} bytes < minimum {min_bytes}"
    if kind == "zip":
        if body[:2] != b"PK" or not zipfile.is_zipfile(io.BytesIO(body)):
            return STATUS_ERROR, "payload is not a valid zip archive"
    else:
        head = body[:512].lstrip().lower()
        if head.startswith(b"<") or b"<html" in head:
            return STATUS_ERROR, "payload looks like HTML, not CSV"
    return None, None


class Connector:
    """Base class. Subclasses set ``source``, ``kind``, ``url``, ``filename``."""

    source: str = "base"
    kind: str = "csv"  # 'csv' | 'zip'
    url: Optional[str] = None
    filename: str = "payload.bin"
    min_bytes: int = 500
    timeout_s: float = 20.0
    min_interval_s: float = 1.0
    max_attempts: int = 2  # one retry max

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        raw_root: Optional[Path | str] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.session = session
        self.raw_root = Path(raw_root) if raw_root is not None else DEFAULT_RAW_ROOT
        self._sleep = sleep
        self._clock = clock
        self._last_request: Optional[float] = None

    # ----- network -------------------------------------------------------
    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval_s - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _get(self, url: str):
        """Return (response, None) or (None, error_note). Never raises.

        Retries once on transport exceptions, 429 and 5xx.
        """
        last_err = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - connector must not raise
                last_err = f"network error on attempt {attempt}: {type(exc).__name__}: {exc}"
                log.warning("%s: %s", self.source, last_err)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code} on attempt {attempt}"
                log.warning("%s: %s", self.source, last_err)
                continue
            return resp, None
        return None, last_err

    # ----- persistence ---------------------------------------------------
    def _save(self, name: str, body: bytes) -> Path:
        folder = self.raw_root / self.source
        folder.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = folder / f"{ts}_{name}"
        n = 2
        while path.exists():  # append-only: never overwrite a raw snapshot
            path = folder / f"{ts}-{n}_{name}"
            n += 1
        path.write_bytes(body)
        return path

    def _record(self, url, status, note, content_type=None, body=b"", raw_path=None) -> SourceRecord:
        return SourceRecord(
            source=self.source,
            url=url,
            fetched_at=utc_now_iso(),
            content_type=content_type,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest() if body else None,
            raw_path=str(raw_path) if raw_path else None,
            status=status,
            note=note,
        )

    def _retrieve(self, url: str, name: str) -> Tuple[SourceRecord, Optional[int]]:
        """Fetch one URL, validate, save if valid.

        Returns (record, http_status). http_status is None when no HTTP
        response was received (network failure).
        """
        resp, err = self._get(url)
        if resp is None:
            return self._record(url, STATUS_UNAVAILABLE, err), None

        http_status = resp.status_code
        ct = resp.headers.get("Content-Type") if resp.headers else None
        body = resp.content or b""
        if http_status != 200:
            return self._record(url, STATUS_UNAVAILABLE, f"HTTP {http_status}", ct, body), http_status

        status, reason = validate_payload(self.kind, ct, body, self.min_bytes)
        if reason is not None:
            log.warning("%s: rejected %s: %s", self.source, url, reason)
            return self._record(url, status, reason, ct, body), http_status

        path = self._save(name, body)
        return self._record(url, STATUS_OK, None, ct, body, path), http_status

    # ----- public --------------------------------------------------------
    def fetch(self) -> SourceRecord:
        if not self.url:
            raise NotImplementedError(f"{type(self).__name__} must set url or override fetch()")
        record, _ = self._retrieve(self.url, self.filename)
        return record
