"""One live fetch each of the NSE debt universe and the latest CM bhavcopy, plus BSE stub.

Usage: .venv/bin/python scripts/smoke_connectors.py
Writes raw files under data/raw/<source>/ (append-only). Makes at most a few requests.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from india_bonds.data_sources.bse import BseDebt  # noqa: E402
from india_bonds.data_sources.nse import NseCmBhavcopy, NseDebtUniverse, csv_rows_from_bytes  # noqa: E402


def main() -> int:
    print("== NSE debt universe ==")
    uni = NseDebtUniverse()
    rec = uni.fetch()
    print(f"status={rec.status} size={rec.size_bytes} content_type={rec.content_type} note={rec.note}")
    if rec.status == "ok":
        rows = uni.parse(csv_rows_from_bytes(Path(rec.raw_path).read_bytes()))
        print(f"parsed corporate DEBT rows={len(rows)} stats={uni.stats}")

    print("== NSE CM bhavcopy (latest, walk-back up to 5 days) ==")
    bhav = NseCmBhavcopy(date.today())
    rec2 = bhav.fetch()
    print(f"status={rec2.status} size={rec2.size_bytes} content_type={rec2.content_type} note={rec2.note}")
    if rec2.status == "ok":
        parsed = bhav.parse_file(rec2.raw_path)
        print(f"debt-series rows={len(parsed)} stats={bhav.stats}")
        print(f"series counts={dict(Counter(r['series'] for r in parsed))}")

    print("== BSE debt ==")
    bse_rec = BseDebt().fetch()
    print(f"status={bse_rec.status} url={bse_rec.url} note={bse_rec.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
