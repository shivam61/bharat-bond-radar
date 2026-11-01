"""BSE debt connector stub.

SPEC: BSE's site returns HTTP 200 text/html for any path and no file URL has
been verified, so this connector makes NO network call. BSE data enters via
CSV import (csv_import.py) only.
"""

from __future__ import annotations

from .base import STATUS_UNAVAILABLE, Connector, SourceRecord, utc_now_iso

BSE_NOTE = "no verified BSE file URL; use CSV import"


class BseDebt(Connector):
    source = "bse_debt"

    def fetch(self) -> SourceRecord:
        return SourceRecord(
            source=self.source,
            url=None,
            fetched_at=utc_now_iso(),
            content_type=None,
            size_bytes=0,
            sha256=None,
            raw_path=None,
            status=STATUS_UNAVAILABLE,
            note=BSE_NOTE,
        )
