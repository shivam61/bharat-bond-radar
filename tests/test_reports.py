"""Tests for india_bonds.reports.markdown and scripts/make_report.py.

All ISINs, issuers, ratings and prices here are SYNTHETIC test data.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from india_bonds.database import db
from india_bonds.dashboard import queries as q
from india_bonds.reports import markdown as rm
from india_bonds.screening.screener import run_screen

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "make_report.py"
SETTLE = date(2026, 10, 30)
DAY = "2026-10-30"
SOURCE = "SYNTHETIC TEST DATA"

PRIME_HI = "INE000R00001"    # AAA, annual 9%, marketplace vendor_ytm 11.1, exec verified, pipe issuer
PRIME_MON = "INE000R00002"   # AA+, monthly 10%, price 900, not executable, offered
PRIME_ZERO = "INE000R00008"  # AA, zero coupon with no coupon_pct -> master incomplete -> LOW confidence
SPEC_Q = "INE000R00003"      # A+, quarterly 11%, price 950, exec verified
SPEC_A = "INE000R00004"      # A, annual 12%, no availability row
REJ_MAT = "INE000R00005"     # matured
REJ_BBB = "INE000R00006"     # BBB; name contains a newline
REJ_NOP = "INE000R00007"     # no price

PRIMARY = {PRIME_HI, PRIME_MON, PRIME_ZERO}
SPECULATIVE = {SPEC_Q, SPEC_A}
REJECTED = {REJ_MAT, REJ_BBB, REJ_NOP}
FORBIDDEN = ("strong buy", "buy candidate", "recommend")

BOND = dict(
    name="SYNTHETIC BOND", exchange_listed=1, instrument_type="NCD", face_value=1000.0,
    coupon_pct=9.0, coupon_frequency="annual", first_interest_date=None,
    maturity_date="2030-10-30", issue_date="2025-10-30", security_type="secured",
    seniority="senior", collateral_text=None, is_perpetual=0, is_at1=0, is_structured=0,
    default_flag=0, trading_suspended=0, currency="INR", source_url=SOURCE, retrieved_on="2026-10-31",
)


def _bond(conn, isin, issuer, **over):
    fields = dict(BOND, issuer_id=db.upsert_issuer(conn, issuer))
    fields.update(over)
    db.upsert_instrument(conn, isin, **fields)


def _rating(conn, isin, rating, agency="CRISIL"):
    db.insert_rating(conn, isin=isin, agency=agency, rating=rating, outlook=None, action="assigned",
                     action_date="2026-01-15", rationale_url=None, source_id=None,
                     source_type="manual", source_ref=SOURCE)


def _price(conn, isin, clean, trade_date="2026-10-29", kind="exchange_close", platform=None,
           vendor_ytm=None, observed_at=None):
    db.insert_price_snapshot(conn, isin=isin, trade_date=trade_date, clean_price=clean, volume=None,
                             value=None, trades=None, kind=kind, platform=platform,
                             vendor_ytm=vendor_ytm, freshness="delayed", source_id=None,
                             observed_at=observed_at, source_url=SOURCE)


def _avail(conn, isin, *, executable, offered, observed="2026-10-29T10:00:00Z", min_inv=None):
    db.insert_availability(conn, isin=isin, listed=1, recently_traded=1, offered_for_sale=offered,
                           price_verified=1, executable_verified=executable, min_investment=min_inv,
                           platform=None, observed_at=observed, source_id=None, source_url=SOURCE)


def _seed_rejects(c):
    _bond(c, REJ_MAT, "SYNTHETIC ISSUER E", maturity_date="2026-10-01")
    _rating(c, REJ_MAT, "AA+")
    _price(c, REJ_MAT, 990.0)

    _bond(c, REJ_BBB, "SYNTHETIC ISSUER F", name="LINE ONE\nLINE TWO")
    _rating(c, REJ_BBB, "BBB")
    _price(c, REJ_BBB, 990.0)

    _bond(c, REJ_NOP, "SYNTHETIC ISSUER G")
    _rating(c, REJ_NOP, "AA+")


def _seed(c):
    _bond(c, PRIME_HI, "SYNTHETIC | PIPE ISSUER")
    _rating(c, PRIME_HI, "AAA")
    _price(c, PRIME_HI, 980.0)
    _price(c, PRIME_HI, 985.0, trade_date=None, kind="marketplace", platform="SYNTHETIC PLATFORM",
           vendor_ytm=11.1, observed_at="2026-10-30T09:00:00Z")
    _avail(c, PRIME_HI, executable=1, offered=0, min_inv=10000.0)

    _bond(c, PRIME_MON, "SYNTHETIC ISSUER B", coupon_pct=10.0, coupon_frequency="monthly",
          maturity_date="2029-10-30")
    _rating(c, PRIME_MON, "AA+")
    _price(c, PRIME_MON, 900.0)
    _avail(c, PRIME_MON, executable=0, offered=1)

    _bond(c, PRIME_ZERO, "SYNTHETIC ISSUER H", coupon_pct=None, coupon_frequency="zero",
          maturity_date="2029-10-30")
    _rating(c, PRIME_ZERO, "AA")
    _price(c, PRIME_ZERO, 950.0)
    _avail(c, PRIME_ZERO, executable=1, offered=0)

    _bond(c, SPEC_Q, "SYNTHETIC ISSUER C", coupon_pct=11.0, coupon_frequency="quarterly",
          maturity_date="2028-10-30")
    _rating(c, SPEC_Q, "A+")
    _price(c, SPEC_Q, 950.0)
    _avail(c, SPEC_Q, executable=1, offered=0)

    _bond(c, SPEC_A, "SYNTHETIC ISSUER D", coupon_pct=12.0, maturity_date="2031-10-30")
    _rating(c, SPEC_A, "A")
    _price(c, SPEC_A, 1000.0)

    _seed_rejects(c)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "reports.sqlite")
    db.init_schema(c)
    _seed(c)
    run_screen(c, SETTLE)  # run_id 'screen-2026-10-30'
    yield c
    c.close()


def _gen(conn, out, run_id=None, generated_at="FIXED-TEST-TIME"):
    return rm.write_report(conn, out, run_id=run_id, generated_at=generated_at)


def _files_on_disk(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:] if nxt == -1 else text[start:nxt]


# ---------------------------------------------------------------- formatting helpers

def test_md_escapes_pipes_and_newlines_and_marks_missing():
    assert rm.md("a|b\nc") == "a\\|b c"
    assert rm.md(None) == "—"
    assert rm.md(float("nan")) == "—"
    assert rm.md("") == "—"


def test_num_is_fixed_two_decimals_and_never_defaults():
    assert rm.num(11.1) == "11.10"
    assert rm.num(None) == "—"
    assert rm.num(float("nan")) == "—"
    assert rm.csv_num(None) == ""


# ---------------------------------------------------------------- layout and separation

def test_file_set_and_folder_date(conn, tmp_path):
    out = tmp_path / "reports"
    res = _gen(conn, out)
    day = out / DAY
    assert res.day_dir == day and day.is_dir()
    expected = {"README.md", "rejected.md", "shortlist.csv", "speculative.csv", "rejected.csv",
                "summary.json"} | {f"bonds/{i}.md" for i in PRIMARY | SPECULATIVE}
    assert set(res.files) == expected
    assert _files_on_disk(day) == expected
    assert (out / "INDEX.md").is_file()


def test_custom_run_id_uses_stored_settlement_date(conn, tmp_path):
    run_screen(conn, SETTLE, run_id="run-custom")
    res = _gen(conn, tmp_path / "reports", run_id="run-custom")
    assert res.day_dir.name == SETTLE.isoformat()


def test_legacy_run_without_settlement_falls_back_to_created_at(conn, tmp_path):
    run_screen(conn, SETTLE, run_id="run-legacy")
    conn.execute("UPDATE screen_result SET settlement = NULL WHERE run_id = 'run-legacy'")
    created = conn.execute(
        "SELECT MAX(created_at) FROM screen_result WHERE run_id = 'run-legacy'").fetchone()[0]
    res = _gen(conn, tmp_path / "reports", run_id="run-legacy")
    assert res.day_dir.name == created[:10]
    assert "taken from the run's created_at date" in _read(res.day_dir / "README.md")


def test_primary_and_speculative_are_separate(conn, tmp_path):
    out = tmp_path / "reports"
    res = _gen(conn, out)
    day = out / DAY
    prim_csv = {r["isin"] for r in _csv_rows(day / "shortlist.csv")}
    spec_csv = {r["isin"] for r in _csv_rows(day / "speculative.csv")}
    assert prim_csv == PRIMARY
    assert spec_csv == SPECULATIVE
    readme = _read(day / "README.md")
    prim_block = _section(readme, "## Primary shortlist")
    spec_block = _section(readme, "## Speculative watchlist (A+/A)")
    for isin in SPECULATIVE:
        assert isin not in prim_block
    for isin in PRIMARY:
        assert isin not in spec_block
    for isin in PRIMARY | SPECULATIVE:
        assert (day / "bonds" / f"{isin}.md").is_file()
    for isin in REJECTED:
        assert not (day / "bonds" / f"{isin}.md").exists()
    assert res.settlement == DAY


def test_vendor_ytm_is_separate_column(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    rows = {r["isin"]: r for r in _csv_rows(day / "shortlist.csv")}
    header = set(_csv_rows(day / "shortlist.csv")[0].keys())
    assert {"our_ytm_pct", "vendor_ytm_pct"} <= header
    assert rows[PRIME_HI]["vendor_ytm_pct"] == "11.10"
    assert rows[PRIME_HI]["our_ytm_pct"] != "11.10"
    assert rows[PRIME_HI]["our_ytm_pct"] != ""
    page = _read(day / "bonds" / f"{PRIME_HI}.md")
    assert "| Vendor YTM (unverified) (%) | 11.10 |" in page
    assert "| Our computed YTM (%) |" in page
    assert "Vendor YTM (unverified) (%)" in _read(day / "README.md")


def test_availability_flags_shown_separately(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    page = _read(day / "bonds" / f"{PRIME_MON}.md")
    assert "| Executable verified | No |" in page
    assert "| Offered for sale | Yes |" in page
    assert "## Availability (separate flags, not combined)" in page


def test_not_assessed_present_and_no_forbidden_wording(conn, tmp_path):
    out = tmp_path / "reports"
    _gen(conn, out)
    day = out / DAY
    for isin in PRIMARY | SPECULATIVE:
        assert rm.NOT_ASSESSED in _read(day / "bonds" / f"{isin}.md")
    texts = [_read(p) for p in day.rglob("*.md")] + [_read(out / "INDEX.md")]
    texts += [_read(p) for p in day.glob("*.csv")]
    for text in texts:
        low = text.lower()
        for word in FORBIDDEN:
            assert word not in low, word


def test_disclaimer_and_freshness_in_readme(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    readme = _read(day / "README.md")
    assert rm.DISCLAIMER in readme
    # PRIME_HI's chosen quote is the marketplace snapshot observed 2026-10-30 (latest on/before settlement)
    assert "latest price_date used = 2026-10-30" in readme
    assert "- **Generated at:** FIXED-TEST-TIME" in readme


def test_low_confidence_marked(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    readme = _read(day / "README.md")
    zero_line = next(line for line in readme.splitlines() if PRIME_ZERO in line)
    assert "**LOW CONFIDENCE**" in zero_line
    hi_line = next(line for line in readme.splitlines() if PRIME_HI in line)
    assert "LOW CONFIDENCE" not in hi_line
    assert "**LOW CONFIDENCE**" in _read(day / "bonds" / f"{PRIME_ZERO}.md")
    assert "**Confidence: High**" in _read(day / "bonds" / f"{PRIME_HI}.md")


def test_bond_page_content(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    page = _read(day / "bonds" / f"{PRIME_HI}.md")
    for heading in ("## Key facts", "## Instrument ratings", "## Pricing", "## Availability",
                    "## Why it passed", "## Concerns & flags",
                    "## How principal is expected to be repaid", "## Sources"):
        assert heading in page
    assert "| Issuer | SYNTHETIC \\| PIPE ISSUER |" in page
    assert "| Lowest instrument rating | AAA |" in page
    assert "Latest price dated 2026-10-30 (marketplace)" in page


def test_income_and_discount_sections(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    readme = _read(day / "README.md")
    income = _section(readme, "## Monthly & quarterly income")
    assert PRIME_MON in income and SPEC_Q in income
    assert PRIME_HI not in income and SPEC_A not in income
    disc = _section(readme, "## Discounted bonds")
    assert PRIME_MON in disc and SPEC_Q in disc
    assert PRIME_HI not in disc


# ---------------------------------------------------------------- rejected.md and escaping

def _table_pipe_counts(text: str) -> list[int]:
    """Unescaped-pipe count for every table line; a consistent table has one count per block."""
    counts = []
    for line in text.splitlines():
        if line.startswith("|"):
            counts.append(len(re.findall(r"(?<!\\)\|", line)))
    return counts


def test_pipe_escaping_keeps_tables_well_formed(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    readme = _read(day / "README.md")
    assert "SYNTHETIC \\| PIPE ISSUER" in readme
    assert "SYNTHETIC | PIPE" not in readme
    lines = readme.splitlines()
    blocks, current = [], []
    for line in lines:
        if line.startswith("|"):
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    for block in blocks:
        counts = {len(re.findall(r"(?<!\\)\|", ln)) for ln in block}
        assert len(counts) == 1, block


def test_newline_in_name_stays_on_one_table_row(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    rej = _read(day / "rejected.md").splitlines()
    assert any("LINE ONE" in ln and "LINE TWO" in ln and ln.startswith("|") for ln in rej)


def test_rejected_md_lists_every_rejected_isin_and_csv_is_full(conn, tmp_path):
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    rej_md = _read(day / "rejected.md")
    for isin in REJECTED:
        assert isin in rej_md
    assert "| Rule | Rejected ISINs |" in rej_md
    csv_isins = {r["isin"] for r in _csv_rows(day / "rejected.csv")}
    assert csv_isins == REJECTED


def test_rejected_listing_is_capped_for_large_runs(conn, tmp_path):
    for i in range(2001):
        conn.execute(
            "INSERT INTO rejection (run_id, isin, rule, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"screen-{DAY}", f"INEBIG{i:05d}", "stale_price", "synthetic bulk row", "2026-10-30"),
        )
    conn.commit()
    day = tmp_path / "reports" / DAY
    _gen(conn, tmp_path / "reports")
    rej_md = _read(day / "rejected.md")
    assert "Listing capped to the first 500 of 2004 rejected ISINs" in rej_md
    # ISIN order: the three seeded INE000R... rejects sort before INEBIG..., so 497 bulk rows fill the cap.
    listed = [ln for ln in rej_md.splitlines() if ln.startswith("| INE")]
    assert len(listed) == 500
    assert sum(1 for ln in listed if ln.startswith("| INEBIG")) == 497
    assert all(isin in rej_md for isin in REJECTED)
    assert len(_csv_rows(day / "rejected.csv")) == 2004


# ---------------------------------------------------------------- empty runs

def test_empty_run_sections_say_none(tmp_path):
    c = db.connect(tmp_path / "rejects_only.sqlite")
    db.init_schema(c)
    _seed_rejects(c)
    run_screen(c, SETTLE)
    out = tmp_path / "reports"
    _gen(c, out)
    day = out / DAY
    readme = _read(day / "README.md")
    for heading in ("## Primary shortlist", "## Speculative watchlist (A+/A)",
                    "## Monthly & quarterly income", "## Discounted bonds"):
        assert rm.NONE_THIS_RUN in _section(readme, heading)
    assert not any((day / "bonds").iterdir())
    assert _csv_rows(day / "shortlist.csv") == []
    assert _read(day / "shortlist.csv").strip() == ",".join(rm.SHORTLIST_CSV_COLUMNS)
    c.close()


def test_no_run_raises(tmp_path):
    c = db.connect(tmp_path / "empty.sqlite")
    db.init_schema(c)
    with pytest.raises(rm.NoRunError):
        _gen(c, tmp_path / "reports")
    c.close()


# ---------------------------------------------------------------- idempotence and index

def test_rerun_same_day_is_deterministic_and_drops_stale_bond_pages(conn, tmp_path):
    out = tmp_path / "reports"
    _gen(conn, out)
    day = out / DAY
    before = {p: p.read_bytes() for p in list(day.rglob("*")) + [out / "INDEX.md"] if p.is_file()}
    (day / "bonds" / "INE000R00999.md").write_text("stale", encoding="utf-8")
    _gen(conn, out)
    after = {p: p.read_bytes() for p in list(day.rglob("*")) + [out / "INDEX.md"] if p.is_file()}
    assert not (day / "bonds" / "INE000R00999.md").exists()
    assert before == after


def test_only_generated_at_line_changes_between_runs(conn, tmp_path):
    out = tmp_path / "reports"
    _gen(conn, out, generated_at="2026-11-01 10:00:00 UTC")
    day = out / DAY
    first = _read(day / "README.md")
    _gen(conn, out, generated_at="2026-11-02 10:00:00 UTC")
    second = _read(day / "README.md")
    assert first != second
    strip = lambda s: [ln for ln in s.splitlines() if not ln.startswith("- **Generated at:**")]
    assert strip(first) == strip(second)


def test_index_lists_newest_first(conn, tmp_path):
    run_screen(conn, date(2026, 10, 29))  # run_id 'screen-2026-10-29'
    out = tmp_path / "reports"
    _gen(conn, out, run_id="screen-2026-10-29")
    _gen(conn, out, run_id=f"screen-{DAY}")
    index = _read(out / "INDEX.md")
    assert index.index(f"({DAY}/README.md)") < index.index("(2026-10-29/README.md)")
    summary = json.loads(_read(out / DAY / "summary.json"))
    assert summary["counts"]["primary"] == len(PRIMARY)
    assert summary["counts"]["speculative"] == len(SPECULATIVE)
    assert summary["counts"]["rejected"] == len(REJECTED)


def test_index_with_no_day_folders(tmp_path):
    out = tmp_path / "empty_reports"
    path = rm.rebuild_index(out)
    assert "No reports yet." in _read(path)


# ---------------------------------------------------------------- CLI

def _make_db(path: Path, seeded: bool) -> None:
    c = db.connect(path)
    db.init_schema(c)
    if seeded:
        _seed(c)
        run_screen(c, SETTLE)
    c.close()


def test_cli_writes_reports(tmp_path):
    dbp = tmp_path / "bonds.db"
    _make_db(dbp, seeded=True)
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(dbp), "--out", str(out)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / DAY / "README.md").is_file()
    assert (out / "INDEX.md").is_file()


def test_cli_exit_codes(tmp_path):
    missing = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o")],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert missing.returncode == 2
    assert not (tmp_path / "nope.db").exists()

    empty_db = tmp_path / "empty.db"
    _make_db(empty_db, seeded=False)
    no_run = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(empty_db), "--out", str(tmp_path / "o")],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert no_run.returncode == 1
    assert "no screening run" in no_run.stderr


def test_sources_table_hides_local_paths_and_blank_rows():
    import pandas as pd
    from india_bonds.reports.markdown import _sources_table
    df = pd.DataFrame([
        {"origin": "instrument master (source_record)", "url": "/home/someone/proj/data/master.csv",
         "fetched_at": "2026-11-01", "status": "ok"},
        {"origin": "availability (source_record)", "url": None, "fetched_at": None, "status": None},
        {"origin": "price", "url": "https://nsearchives.nseindia.com/x.zip", "fetched_at": "2026-11-01", "status": "ok"},
    ])
    out = _sources_table({"sources": df})
    assert "/home/someone" not in out and "file: master.csv" in out
    assert "nsearchives.nseindia.com" in out and "availability (source_record)" not in out
