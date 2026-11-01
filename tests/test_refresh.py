"""Tests for india_bonds.refresh and scripts/refresh.py. No network: connectors are fakes that
read repo fixtures and write into tmp_path. Uses SYNTHETIC rows only."""

from __future__ import annotations

import csv
import io
import itertools
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from india_bonds.data_sources import csv_import as ci
from india_bonds.data_sources.base import STATUS_OK, SourceRecord
from india_bonds.data_sources.bse import BseDebt
from india_bonds.data_sources.nse import URL_UNIVERSE, NseCmBhavcopy, NseDebtUniverse, csv_rows_from_bytes
from india_bonds.refresh import (
    NO_MASTER_WARNING,
    DatabaseOpenError,
    format_summary,
    init_inputs,
    run_refresh,
)

FIXTURES = Path(__file__).parent / "fixtures"
UNIVERSE_BYTES = (FIXTURES / "nse_active_debt_excerpt.csv").read_bytes()
BHAV_BYTES = (FIXTURES / "nse_bhav_excerpt.csv").read_bytes()
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh.py"

FETCHED = "2026-11-01T18:16:14+00:00"
SETTLEMENT = date(2026, 10, 31)   # one day after the fixture trade date -> walk-back warning
TRADE_DATE = date(2026, 10, 30)
AFIL = "INE916Y07057"             # present in the bhavcopy excerpt

BHAV_ISINS = [r["isin"] for r in NseCmBhavcopy(TRADE_DATE).parse_rows(csv_rows_from_bytes(BHAV_BYTES))]
# The universe excerpt and bhavcopy excerpt share no ISINs, so the fake universe appends the
# bhavcopy ISINs as SYNTHETIC DEBT rows (ISINs are the fixture's own; names are synthetic).
UNIVERSE_BODY = UNIVERSE_BYTES + "".join(
    f"{9000 + i},SYNTHETIC DEBT {i},{isin},DEBT\n" for i, isin in enumerate(BHAV_ISINS)
).encode("utf-8")

# SYNTHETIC instrument master row (no real issuer data).
MASTER_ROW = [
    "INE000T00101", "SYNTHETIC ISSUER K", "SYNTHETIC K", "1000", "9.00", "annual",
    "2026-03-15", "2028-03-15", "2025-03-15", "secured", "senior", "SYNTHETIC collateral",
    "false", "false", "false", "false", "false", "INR", "SYNTHETIC TEST DATA", "2026-10-31",
]

_COUNTER = itertools.count(1)


def _save_raw(raw_root: Path, source: str, name: str, body: bytes) -> Path:
    folder = raw_root / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{next(_COUNTER):06d}_{name}"
    path.write_bytes(body)
    return path


class FakeUniverse:
    source = NseDebtUniverse.source

    def __init__(self, raw_root: Path, ok: bool = True):
        self.raw_root = raw_root
        self.ok = ok

    def fetch(self) -> SourceRecord:
        if not self.ok:
            return SourceRecord(self.source, URL_UNIVERSE, FETCHED, None, 0, None, None,
                                "unavailable", "simulated outage")
        path = _save_raw(self.raw_root, self.source, "List_of_Active_Securities_CM_DEBT.csv", UNIVERSE_BODY)
        return SourceRecord(self.source, URL_UNIVERSE, FETCHED, "text/csv", len(UNIVERSE_BODY),
                            _sha(UNIVERSE_BODY), str(path), STATUS_OK, None)

    def parse_file(self, path):
        return NseDebtUniverse().parse_file(path)


class FakeBhav:
    source = NseCmBhavcopy.source
    url = "https://example.invalid/BhavCopy_NSE_CM_fake.csv.zip"

    def __init__(self, raw_root: Path, ok: bool = True, body: bytes | None = None,
                 resolved: date | None = TRADE_DATE):
        self.raw_root = raw_root
        self.ok = ok
        self.body = body if body is not None else BHAV_BYTES
        self.resolved_date = resolved if ok else None

    def fetch(self) -> SourceRecord:
        if not self.ok:
            return SourceRecord(self.source, self.url, FETCHED, None, 0, None, None,
                                "unavailable", "simulated outage")
        path = _save_raw(self.raw_root, self.source, "bhav.csv", self.body)
        return SourceRecord(self.source, self.url, FETCHED, "application/zip", len(self.body),
                            _sha(self.body), str(path), STATUS_OK, None)

    def parse_file(self, path):
        return NseCmBhavcopy(TRADE_DATE).parse_rows(csv_rows_from_bytes(Path(path).read_bytes()))


class MustNotRun:
    source = "must_not_run"

    def fetch(self):
        raise AssertionError("network connector must not run in offline mode")


def _sha(body: bytes) -> str:
    import hashlib
    return hashlib.sha256(body).hexdigest()


def modified_bhav_body(isin: str, new_close: str) -> bytes:
    """The bhavcopy excerpt with one ISIN's ClsPric changed (all other cells identical)."""
    reader = csv.DictReader(io.StringIO(BHAV_BYTES.decode("utf-8-sig")))
    rows = list(reader)
    for row in rows:
        if row["ISIN"] == isin:
            row["ClsPric"] = new_close
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def _run(tmp_path, *, universe=None, bhav=None, bse=None, offline=False, inputs=None,
         make_report=True, settlement=SETTLEMENT, corp_trades=False):
    # corp_trades=False: the NSE OTC/RFQ source is not exercised here (no network in tests);
    # its behaviour is covered with injected fakes in tests/test_corp_trades.py.
    raw = tmp_path / "raw"
    connectors = None if offline else {
        "universe": universe or FakeUniverse(raw),
        "bhavcopy": bhav or FakeBhav(raw),
        "bse": bse or BseDebt(),
    }
    return run_refresh(
        tmp_path / "data" / "bonds.db", settlement, offline,
        inputs if inputs is not None else tmp_path / "inputs",
        tmp_path / "reports", make_report, connectors=connectors, corp_trades=corp_trades,
    )


TABLES = ("instrument", "price_snapshot", "availability", "screen_result", "rejection", "source_record")


def _counts(tmp_path) -> dict:
    c = sqlite3.connect(tmp_path / "data" / "bonds.db")
    out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    c.close()
    return out


def _raw_files(tmp_path, source: str) -> int:
    folder = tmp_path / "raw" / source
    return len(list(folder.iterdir())) if folder.exists() else 0


def test_happy_run_writes_db_rows_screen_and_report(tmp_path):
    s = _run(tmp_path)

    # universe, bhavcopy, corp trades (skipped by _run), bse
    assert [src.status for src in s.sources] == ["ok", "ok", "skipped", "unavailable"]
    assert s.universe_rows and s.universe_rows > 0
    assert s.bhavcopy_trade_date == TRADE_DATE.isoformat()
    assert s.bhavcopy_walkback_days == 1
    assert s.bhav_new and s.bhav_new > 0
    assert s.bhav_changed == 0
    assert s.bhav_availability_written and s.bhav_availability_written > 0
    assert any("bhavcopy walked back 1 day(s)" in w for w in s.warnings)
    assert NO_MASTER_WARNING in s.warnings

    counts = _counts(tmp_path)
    assert counts["instrument"] > 0
    assert counts["price_snapshot"] > 0
    assert counts["screen_result"] > 0

    day = Path(s.report_dir)
    assert day.name == SETTLEMENT.isoformat()
    assert (day / "README.md").is_file()
    assert (day / "summary.json").is_file()

    text = format_summary(s)
    assert "WARNINGS (" in text and "report: " in text and "DB writes:" in text


def test_unavailable_sources_complete_with_warnings_and_stored_data(tmp_path):
    raw = tmp_path / "raw"
    _run(tmp_path)  # populate the DB first
    before = _counts(tmp_path)

    s = _run(tmp_path, universe=FakeUniverse(raw, ok=False), bhav=FakeBhav(raw, ok=False))

    statuses = {src.name: src.status for src in s.sources}
    assert statuses[NseDebtUniverse.source] == "unavailable"
    assert statuses[NseCmBhavcopy.source] == "unavailable"
    assert any(w.startswith(f"{NseDebtUniverse.source} unavailable") for w in s.warnings)
    assert any(w.startswith(f"{NseCmBhavcopy.source} unavailable") for w in s.warnings)

    c = sqlite3.connect(tmp_path / "data" / "bonds.db")
    unavailable = {r[0] for r in c.execute("SELECT source FROM source_record WHERE status = 'unavailable'")}
    c.close()
    assert {NseDebtUniverse.source, NseCmBhavcopy.source, BseDebt.source} <= unavailable

    after = _counts(tmp_path)
    assert after["instrument"] == before["instrument"]
    assert after["price_snapshot"] == before["price_snapshot"]
    assert after["screen_result"] > 0  # screened from stored data
    assert s.report_dir is not None


def test_identical_second_run_writes_no_rows(tmp_path):
    s1 = _run(tmp_path)
    c1 = _counts(tmp_path)
    assert s1.db_writes > 0

    s2 = _run(tmp_path)
    c2 = _counts(tmp_path)

    assert c2["source_record"] == c1["source_record"]
    assert c2["price_snapshot"] == c1["price_snapshot"]
    assert c2["availability"] == c1["availability"]
    assert c2["instrument"] == c1["instrument"]
    assert c2["rejection"] == c1["rejection"]
    assert c2["screen_result"] == c1["screen_result"]

    assert s2.db_writes == 0
    assert s2.bhavcopy_file_unchanged is True
    assert s2.screen_reused is True
    universe = next(src for src in s2.sources if src.name == NseDebtUniverse.source)
    assert universe.note.startswith("unchanged since ")
    assert s2.bhav_new is None  # no price rows processed for an identical file
    text = format_summary(s2)
    assert "DB unchanged" in text
    assert "unchanged since" in text

    # Duplicate raw files are deleted, so each source keeps exactly one raw snapshot.
    assert _raw_files(tmp_path, NseDebtUniverse.source) == 1
    assert _raw_files(tmp_path, NseCmBhavcopy.source) == 1


def test_modified_price_inserts_exactly_the_changed_row(tmp_path):
    raw = tmp_path / "raw"
    s1 = _run(tmp_path)
    c1 = _counts(tmp_path)

    body = modified_bhav_body(AFIL, "9900.00")
    s2 = _run(tmp_path, bhav=FakeBhav(raw, body=body))
    c2 = _counts(tmp_path)

    assert s2.bhavcopy_file_unchanged is False
    assert s2.bhav_changed == 1
    assert s2.bhav_new == 0
    assert s2.bhav_unchanged == s1.bhav_new - 1
    assert c2["price_snapshot"] - c1["price_snapshot"] == 1
    assert c2["availability"] == c1["availability"]        # flags unchanged -> no availability row
    assert c2["source_record"] - c1["source_record"] == 1  # the new bhavcopy file itself
    assert c2["instrument"] == c1["instrument"]

    c = sqlite3.connect(tmp_path / "data" / "bonds.db")
    latest = c.execute(
        "SELECT clean_price FROM price_snapshot WHERE isin = ? AND kind = 'exchange_close' "
        "ORDER BY id DESC LIMIT 1", (AFIL,),
    ).fetchone()[0]
    c.close()
    assert latest == pytest.approx(9900.0)
    assert s2.screen_reused is False  # something was written, so the screen is re-run


def test_bse_stub_is_recorded_once_across_identical_runs(tmp_path):
    _run(tmp_path)
    s = _run(tmp_path)
    bse = next(src for src in s.sources if src.name == BseDebt.source)
    assert bse.status == "unavailable"
    c = sqlite3.connect(tmp_path / "data" / "bonds.db")
    n = c.execute("SELECT COUNT(*) FROM source_record WHERE source = ?", (BseDebt.source,)).fetchone()[0]
    c.close()
    assert n == 1


def test_init_inputs_creates_templates_and_never_overwrites(tmp_path):
    d = tmp_path / "inputs"
    first = init_inputs(d)
    assert set(first.values()) == {"created"}

    headers = {"instrument_master.csv": ci.MASTER_HEADER, "ratings.csv": ci.RATINGS_HEADER,
               "marketplace.csv": ci.MARKETPLACE_HEADER}
    for name, header in headers.items():
        with (d / name).open(newline="", encoding="utf-8") as fh:
            assert list(csv.reader(fh)) == [header]

    (d / "ratings.csv").write_text("custom,content\n", encoding="utf-8")
    second = init_inputs(d)
    assert second["ratings.csv"].startswith("exists")
    assert (d / "ratings.csv").read_text(encoding="utf-8") == "custom,content\n"


def test_init_inputs_cli_exits_without_network(tmp_path):
    d = tmp_path / "cli_inputs"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--init-inputs", "--inputs", str(d)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert (d / "instrument_master.csv").is_file()
    assert (d / "marketplace.csv").is_file()


def test_csv_import_errors_are_summarised(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    good = ",".join(MASTER_ROW)
    bad = ",".join(["BADISIN"] + MASTER_ROW[1:])
    lines = [",".join(ci.MASTER_HEADER), good] + [bad] * 12
    (inputs / "instrument_master.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    s = _run(tmp_path, inputs=inputs)

    master = next(c for c in s.csv if c.name == "instrument_master.csv")
    assert master.provided
    assert master.rows_read == 13
    assert master.rows_ok == 1
    assert master.error_count == 12
    assert len(master.errors) == 10  # only the first 10 are kept
    assert master.errors[0].startswith("line 3:")
    assert any("12 row(s) rejected" in w for w in s.warnings)
    assert NO_MASTER_WARNING not in s.warnings

    for name in ("ratings.csv", "marketplace.csv"):
        cs = next(c for c in s.csv if c.name == name)
        assert not cs.provided
    assert "ratings.csv: not provided" in format_summary(s)


def test_unchanged_csv_is_not_reimported(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "instrument_master.csv").write_text(
        ",".join(ci.MASTER_HEADER) + "\n" + ",".join(MASTER_ROW) + "\n", encoding="utf-8")
    _run(tmp_path, inputs=inputs)
    c1 = _counts(tmp_path)

    s2 = _run(tmp_path, inputs=inputs)
    c2 = _counts(tmp_path)

    master = next(c for c in s2.csv if c.name == "instrument_master.csv")
    assert master.unchanged_since is not None
    assert c2["source_record"] == c1["source_record"]
    assert c2["instrument"] == c1["instrument"]


def test_offline_skips_connectors_but_still_screens_and_imports(tmp_path):
    s = _run(tmp_path, offline=True, inputs=tmp_path / "inputs")

    assert all(src.status == "skipped" for src in s.sources)
    assert s.offline is True
    assert s.universe_rows is None
    # Empty DB: nothing to report on, which is a warning not a crash.
    assert s.report_dir is None
    assert any(w.startswith("report skipped") for w in s.warnings)


def test_offline_connector_is_never_called(tmp_path):
    connectors = {"universe": MustNotRun(), "bhavcopy": MustNotRun(), "bse": MustNotRun()}
    run_refresh(tmp_path / "bonds.db", SETTLEMENT, True, tmp_path / "inputs", tmp_path / "reports",
                False, connectors=connectors, corp_trades=False)


def test_unopenable_db_path_raises_database_open_error(tmp_path):
    blocker = tmp_path / "not_a_dir.txt"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(DatabaseOpenError):
        run_refresh(blocker / "bonds.db", SETTLEMENT, True, tmp_path / "inputs",
                    tmp_path / "reports", False)


def test_cli_exit_code_2_on_unopenable_db(tmp_path):
    blocker = tmp_path / "not_a_dir.txt"
    blocker.write_text("x", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(blocker / "bonds.db"), "--date", "2026-10-31",
         "--offline", "--inputs", str(tmp_path / "none"), "--out", str(tmp_path / "reports")],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2
    assert "cannot open database" in proc.stderr


def test_cli_offline_no_report_exits_0(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(tmp_path / "bonds.db"), "--date", "2026-10-31",
         "--offline", "--no-report", "--inputs", str(tmp_path / "none")],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "refresh settlement=2026-10-31" in proc.stdout
