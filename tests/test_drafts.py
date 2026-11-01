"""Draft worksheets from descriptor hints (enrichment/drafts.py, scripts/enrich.py). No network.

SYNTHETIC ISINs and issuers; descriptor strings follow the NSE naming pattern only."""

from __future__ import annotations

import csv
import importlib.util
import socket
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from india_bonds.data_sources import csv_import as ci
from india_bonds.database import db
from india_bonds.enrichment.descriptor import parse_descriptor
from india_bonds.enrichment.drafts import (
    BD_COLUMNS,
    BONDDEKHO_URL,
    CDSL_URL,
    DRAFT_MASTER_NAME,
    MASTER_REVIEW_NAME,
    README_NAME,
    REVIEW_HEADER,
    WORKLIST_CSV_NAME,
    WORKLIST_HEADER,
    WORKLIST_MD_NAME,
    WORKLIST_HEADER_TEXT,
    write_drafts,
)
from india_bonds.screening.screener import run_screen

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "enrich.py"
TODAY = date(2026, 11, 2)
OBSERVED = "2026-11-01T18:00:00+00:00"

# Full hint, no master coupon -> in draft master.
DRAFT_ISIN = "INE000B00001"
DRAFT_NAME = "REC LIMITED SR 261 8.3 BD 31OT31 FVRS1LAC"
# Full hint, perpetual hint, no master coupon -> in draft master with is_perpetual blank.
PERP_ISIN = "INE000B00002"
PERP_NAME = "ALPHA CAPITAL SR P1 9.5 NCD 31DC35 PERP FVRS1LAC"
# Full hint but master already has a coupon -> not in draft master, still in review.
HAS_COUPON_ISIN = "INE000B00003"
HAS_COUPON_NAME = "GAMMA FIN SR 7 8.0 NCD 31OT30 FVRS1LAC"
# Partial hint (no face value) -> review only.
PARTIAL_ISIN = "INE000B00004"
PARTIAL_NAME = "DELTA FIN SR 1 BD 31OT31"
# Issuer-only -> no hint at all.
ISSUER_ISIN = "INE000B00005"
ISSUER_NAME = "EPSILON FINANCE LIMITED"
# Priced hinted bonds for worklist ordering.
PRICED_LOW_ISIN = "INE000B00006"     # yield 8.1, trades 3
PRICED_TIE_ISIN = "INE000B00007"     # yield 9.0, trades 1
PRICED_TIE_TOP_ISIN = "INE000B00008"  # yield 9.0, trades 5
STALE_ISIN = "INE000B00009"          # priced 30 days ago -> outside 7-day window
UNPRICED_ISIN = "INE000B00010"       # hint, no price at all

NAMES = {
    DRAFT_ISIN: DRAFT_NAME, PERP_ISIN: PERP_NAME, HAS_COUPON_ISIN: HAS_COUPON_NAME,
    PARTIAL_ISIN: PARTIAL_NAME, ISSUER_ISIN: ISSUER_NAME,
    PRICED_LOW_ISIN: "OMEGA CAPITAL SR A 8.1 NCD 31OT31 FVRS1LAC",
    PRICED_TIE_ISIN: "OMEGA CAPITAL SR B 9.0 NCD 31OT31 FVRS1LAC",
    PRICED_TIE_TOP_ISIN: "OMEGA CAPITAL SR C 9.0 NCD 31OT31 FVRS1LAC",
    STALE_ISIN: "OMEGA CAPITAL SR D 9.2 NCD 31OT31 FVRS1LAC",
    UNPRICED_ISIN: "OMEGA CAPITAL SR E 7.9 NCD 31OT31 FVRS1LAC",
}


def _db(tmp_path, *, master_coupon_for=(HAS_COUPON_ISIN,)) -> sqlite3.Connection:
    path = tmp_path / "bonds.db"
    conn = db.connect(path)
    db.init_schema(conn)
    for isin, name in NAMES.items():
        coupon = 9.0 if isin in master_coupon_for else None
        db.upsert_instrument(conn, isin, name=name, instrument_type="DEBT", exchange_listed=1,
                             coupon_pct=coupon)
        if name and isin in (DRAFT_ISIN, PERP_ISIN, HAS_COUPON_ISIN, PARTIAL_ISIN, PRICED_LOW_ISIN,
                             PRICED_TIE_ISIN, PRICED_TIE_TOP_ISIN, STALE_ISIN, UNPRICED_ISIN):
            db.upsert_descriptor_hint(conn, isin, parse_descriptor(name), None, OBSERVED)
    prices = [
        (PRICED_LOW_ISIN, TODAY - timedelta(days=1), 8.1, 3),
        (PRICED_TIE_ISIN, TODAY - timedelta(days=2), 9.0, 1),
        (PRICED_TIE_TOP_ISIN, TODAY - timedelta(days=3), 9.0, 5),
        (STALE_ISIN, TODAY - timedelta(days=30), 9.2, 9),
    ]
    for isin, when, yld, trades in prices:
        conn.execute(
            "INSERT INTO price_snapshot (isin, trade_date, kind, price_per_100, reported_yield_pct, trades) "
            "VALUES (?, ?, 'exchange_reported', ?, ?, ?)",
            (isin, when.isoformat(), 99.5, yld, trades),
        )
    conn.commit()
    return conn


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write(tmp_path, **kw):
    conn = _db(tmp_path, **kw)
    out = tmp_path / "drafts"
    summary = write_drafts(conn, out, today=TODAY, max_age_days=7)
    return conn, out, summary


def test_master_draft_has_exact_header_only_eligible_isins_and_blanks(tmp_path):
    conn, out, s = _write(tmp_path)
    text = (out / DRAFT_MASTER_NAME).read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(ci.MASTER_HEADER)
    rows = _rows(out / DRAFT_MASTER_NAME)
    isins = [r["isin"] for r in rows]
    # Full hint without master coupon only. Partial, issuer-only and already-priced-in-master excluded.
    assert isins == sorted(isins)  # deterministic order
    assert sorted(isins) == sorted({DRAFT_ISIN, PERP_ISIN, PRICED_LOW_ISIN, PRICED_TIE_ISIN,
                                    PRICED_TIE_TOP_ISIN, STALE_ISIN, UNPRICED_ISIN})
    assert HAS_COUPON_ISIN not in isins and PARTIAL_ISIN not in isins and ISSUER_ISIN not in isins

    row = next(r for r in rows if r["isin"] == DRAFT_ISIN)
    assert row["issuer_name"] == "Rec Limited"
    assert row["instrument_name"] == DRAFT_NAME
    assert row["face_value"] == "100000"
    assert row["coupon_pct"] == "8.3"
    assert row["maturity_date"] == "2031-10-31"
    assert row["currency"] == "INR"
    assert row["source_url"] == "NSE descriptor (unverified draft)"
    assert row["retrieved_on"] == "2026-11-01"
    for col in ("coupon_frequency", "first_interest_date", "issue_date", "security_type",
                "seniority", "collateral_text", "is_perpetual", "is_at1", "is_structured",
                "default_flag", "trading_suspended"):
        assert row[col] == "", col
    conn.close()


def test_draft_leaves_perpetual_blank_even_with_hint(tmp_path):
    conn, out, _ = _write(tmp_path)
    row = next(r for r in _rows(out / DRAFT_MASTER_NAME) if r["isin"] == PERP_ISIN)
    assert row["is_perpetual"] == ""
    review = next(r for r in _rows(out / MASTER_REVIEW_NAME) if r["isin"] == PERP_ISIN)
    assert review["perpetual_hint"] == "1"
    conn.close()


def test_draft_import_is_accepted_and_still_rejected_by_screener(tmp_path):
    conn, out, _ = _write(tmp_path)
    report = ci.import_instrument_master(conn, out / DRAFT_MASTER_NAME)
    assert report.errors == []
    assert report.rows_ok == 7  # every draft row: DRAFT, PERP and the five OMEGA ISINs
    counts = run_screen(conn, TODAY, run_id="draft-test")
    assert counts["buckets"]["primary"] == 0 and counts["buckets"]["speculative"] == 0
    for isin in (DRAFT_ISIN, PERP_ISIN):
        bucket = conn.execute(
            "SELECT bucket FROM screen_result WHERE run_id = 'draft-test' AND isin = ?", (isin,)
        ).fetchone()[0]
        assert bucket == "rejected"
        reasons = {r[0] for r in conn.execute(
            "SELECT rule FROM rejection WHERE run_id = 'draft-test' AND isin = ?", (isin,))}
        assert {"no_instrument_rating", "unverified_default_flag"} <= reasons
    conn.close()


def test_writing_drafts_never_changes_instrument_table(tmp_path):
    conn = _db(tmp_path)
    before = conn.execute("SELECT * FROM instrument ORDER BY isin").fetchall()
    write_drafts(conn, tmp_path / "drafts", today=TODAY, max_age_days=7)
    after = conn.execute("SELECT * FROM instrument ORDER BY isin").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    conn.close()


def test_review_rows_list_missing_fields_and_reference_links(tmp_path):
    conn, out, _ = _write(tmp_path)
    rows = _rows(out / MASTER_REVIEW_NAME)
    assert {r["isin"] for r in rows} == set(NAMES) - {ISSUER_ISIN}
    r = next(x for x in rows if x["isin"] == DRAFT_ISIN)
    assert r["cdsl_reference_url"] == CDSL_URL.format(isin=DRAFT_ISIN)
    assert r["bonddekho_reference_url"] == BONDDEKHO_URL.format(isin=DRAFT_ISIN)
    assert r["cdsl_reference_url"] == f"https://www.cdslindia.com/CorporateBond/CorpBondDatabase.aspx?ISIN={DRAFT_ISIN}"
    assert r["bonddekho_reference_url"] == f"https://www.bonddekho.com/bond/{DRAFT_ISIN}"
    for part in ("coupon_frequency", "issue_date or first_interest_date", "instrument rating",
                 "security_type", "seniority", "is_perpetual", "is_at1", "is_structured",
                 "default_flag", "trading_suspended"):
        assert part in r["still_missing"].split("; ")
    assert r["years_to_maturity"] == f"{(date(2031, 10, 31) - TODAY).days / 365.25:.2f}"
    conn.close()


def test_worklist_orders_by_yield_then_trades_and_applies_window(tmp_path):
    conn, out, s = _write(tmp_path)
    work = _rows(out / WORKLIST_CSV_NAME)
    assert [r["isin"] for r in work] == [PRICED_TIE_TOP_ISIN, PRICED_TIE_ISIN, PRICED_LOW_ISIN]
    assert s.worklist_rows == 3
    assert work[0]["issuer_series"] == "Omega Capital SR C"
    assert work[0]["nse_reported_yield_pct_unverified"] == "9"
    assert work[0]["trades"] == "5"
    assert work[0]["price_date"] == (TODAY - timedelta(days=3)).isoformat()
    conn.close()


def test_worklist_markdown_has_header_text_and_table(tmp_path):
    conn, out, _ = _write(tmp_path)
    md = (out / WORKLIST_MD_NAME).read_text(encoding="utf-8")
    assert WORKLIST_HEADER_TEXT in md
    assert "| ISIN | Issuer / series | Coupon hint % |" in md
    table_rows = [line for line in md.splitlines() if line.startswith("| INE")]
    assert [line.split("|")[1].strip() for line in table_rows] == [
        PRICED_TIE_TOP_ISIN, PRICED_TIE_ISIN, PRICED_LOW_ISIN]
    conn.close()


def test_readme_states_currency_basis_and_workflow(tmp_path):
    conn, out, _ = _write(tmp_path)
    text = (out / README_NAME).read_text(encoding="utf-8")
    assert "currency" in text and "INR" in text and "FVRS" in text
    assert "unverified" in text.lower()
    assert "instrument_master.csv" in text
    conn.close()


def test_drafts_are_deterministic_on_rerun(tmp_path):
    conn = _db(tmp_path)
    out = tmp_path / "drafts"
    write_drafts(conn, out, today=TODAY, max_age_days=7)
    first = {n: (out / n).read_bytes() for n in (DRAFT_MASTER_NAME, MASTER_REVIEW_NAME,
                                                  WORKLIST_CSV_NAME, WORKLIST_MD_NAME, README_NAME)}
    write_drafts(conn, out, today=TODAY, max_age_days=7)
    for name, body in first.items():
        assert (out / name).read_bytes() == body, name
    conn.close()


def test_empty_db_writes_header_only_files(tmp_path):
    path = tmp_path / "empty.db"
    conn = db.connect(path)
    db.init_schema(conn)
    out = tmp_path / "drafts"
    s = write_drafts(conn, out, today=TODAY, max_age_days=7)
    assert (s.hinted, s.master_rows, s.review_rows, s.worklist_rows) == (0, 0, 0, 0)
    assert (out / DRAFT_MASTER_NAME).read_text(encoding="utf-8").splitlines() == [",".join(ci.MASTER_HEADER)]
    assert len(_rows(out / MASTER_REVIEW_NAME)) == 0
    assert WORKLIST_HEADER_TEXT in (out / WORKLIST_MD_NAME).read_text(encoding="utf-8")
    conn.close()


def test_db_without_hint_table_is_treated_as_no_hints(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE instrument (isin TEXT PRIMARY KEY, name TEXT)")
    conn.commit()
    s = write_drafts(conn, tmp_path / "drafts", today=TODAY, max_age_days=7)
    assert s.hinted == 0
    conn.close()


def test_cli_draft_exit_codes_and_outputs(tmp_path):
    missing = subprocess.run([sys.executable, str(SCRIPT), "draft", "--db", str(tmp_path / "none.db"),
                              "--out", str(tmp_path / "d")], capture_output=True, text=True)
    assert missing.returncode == 2
    assert "database not found" in missing.stderr

    conn = _db(tmp_path)
    conn.close()
    ok = subprocess.run([sys.executable, str(SCRIPT), "draft", "--db", str(tmp_path / "bonds.db"),
                         "--out", str(tmp_path / "d"), "--max-age-days", "7"],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert "instrument_master_DRAFT.csv rows:" in ok.stdout
    assert (tmp_path / "d" / WORKLIST_MD_NAME).is_file()


def test_worklist_excludes_hinted_matured_bonds():
    from datetime import date
    from india_bonds.enrichment.drafts import worklist
    base = dict(issuer="X LTD", series="SR 1", coupon_hint=9.0, face_hint=1000.0, years_to_maturity=1.0,
                latest_price_date="2026-10-30", nse_reported_yield_pct=10.0, trades=1,
                price_per_100=98.0, still_missing=["coupon_frequency"],
                cdsl_reference_url="x", bonddekho_reference_url="y")
    rows = worklist([
        dict(base, isin="INE000T00001", maturity_hint="2026-05-07"),   # matured -> excluded
        dict(base, isin="INE000T00002", maturity_hint="2027-05-07"),   # kept
        dict(base, isin="INE000T00003", maturity_hint=None),            # unknown maturity -> kept
    ], date(2026, 11, 2), 7)
    assert [r["isin"] for r in rows] == ["INE000T00002", "INE000T00003"]


# ---- BondDekho hint columns (local raw files only; SYNTHETIC values and ISINs) -----------------

OLD_REVIEW_COLUMNS = [
    "isin", "issuer", "series", "coupon_hint", "maturity_hint", "face_hint", "years_to_maturity",
    "perpetual_hint", "at1_hint", "call_hint", "hint_warnings", "latest_price_date",
    "latest_price_kind", "nse_reported_yield_pct", "trades", "still_missing",
    "cdsl_reference_url", "bonddekho_reference_url",
]
OLD_WORKLIST_COLUMNS = [
    "isin", "issuer_series", "coupon_hint", "maturity_hint", "years_left", "face_hint",
    "nse_price_per_100", "nse_reported_yield_pct_unverified", "trades", "price_date",
    "still_missing", "cdsl_reference_url", "bonddekho_reference_url",
]
BD_BLANK_COLUMNS = [c for c in BD_COLUMNS if c != "bd_status"]


def _bd_file(bd_dir: Path, isin: str, *, rating="A (CRISIL)", freq="Monthly", face="Rs. 100000.0",
             maturity="2031-10-31", security="Senior Secured", bond_type="Senior Secured Bond") -> None:
    bd_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join([
        f"## Synthetic Testco Finance Limited — {isin}", "",
        f"- **Type:** {bond_type}",
        f"- **Credit Rating:** {rating}",
        "- **Coupon Rate:** 8.3%",
        f"- **Coupon Frequency:** {freq}",
        f"- **Face Value:** {face}",
        f"- **Maturity Date:** {maturity} (5.0 years to maturity)",
        "- **Issue Date:** 2026-04-21",
        "- **Sector:** NBFC",
        f"- **Security Type:** {security}",
        "",
        "### Available on 1 platform(s):",
        "",
        "- **GoldenPi:** 8.10% YTM, Price: Rs. 99, Min: Rs. 10,000",
        "",
    ]) + "\n"
    (bd_dir / f"{isin}.txt").write_text(text, encoding="utf-8")


def _write_bd(tmp_path, bd_dir):
    conn = _db(tmp_path)
    out = tmp_path / "drafts"
    summary = write_drafts(conn, out, today=TODAY, max_age_days=7, bonddekho_dir=bd_dir)
    return conn, out, summary


def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted during draft write")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _load_enrich_script():
    spec = importlib.util.spec_from_file_location("enrich_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bd_columns_are_appended_after_existing_and_filled(tmp_path):
    bd = tmp_path / "bd"
    _bd_file(bd, DRAFT_ISIN)
    _bd_file(bd, PRICED_TIE_TOP_ISIN)
    conn, out, s = _write_bd(tmp_path, bd)

    review_header = (out / MASTER_REVIEW_NAME).read_text(encoding="utf-8").splitlines()[0].split(",")
    assert review_header == OLD_REVIEW_COLUMNS + BD_COLUMNS
    work_header = (out / WORKLIST_CSV_NAME).read_text(encoding="utf-8").splitlines()[0].split(",")
    assert work_header == OLD_WORKLIST_COLUMNS + BD_COLUMNS
    assert REVIEW_HEADER == review_header and WORKLIST_HEADER == work_header

    r = next(x for x in _rows(out / MASTER_REVIEW_NAME) if x["isin"] == DRAFT_ISIN)
    assert r["bd_status"] == "found"
    assert r["bd_rating_unverified"] == "A"
    assert r["bd_rating_agency_unverified"] == "CRISIL"
    assert r["bd_frequency_unverified"] == "monthly"
    assert r["bd_secured_unverified"] == "secured"
    assert r["bd_seniority_unverified"] == "senior"
    assert r["bd_face_vs_nse"] == "agree"
    assert r["bd_maturity_vs_nse"] == "agree"

    w = next(x for x in _rows(out / WORKLIST_CSV_NAME) if x["isin"] == PRICED_TIE_TOP_ISIN)
    assert w["bd_status"] == "found" and w["bd_frequency_unverified"] == "monthly"
    assert w["bd_face_vs_nse"] == "agree" and w["bd_maturity_vs_nse"] == "agree"
    # Rows without a file are still listed, with status no_file.
    other = next(x for x in _rows(out / WORKLIST_CSV_NAME) if x["isin"] == PRICED_LOW_ISIN)
    assert other["bd_status"] == "no_file"
    assert s.bd_status_counts == {"found": 2, "no_file": 7}  # 9 hinted ISINs
    conn.close()


def test_missing_files_give_no_file_and_blank_bd_values(tmp_path):
    empty = tmp_path / "empty_bd"
    empty.mkdir()
    conn, out, s = _write_bd(tmp_path, empty)
    rows = _rows(out / MASTER_REVIEW_NAME)
    assert rows and all(r["bd_status"] == "no_file" for r in rows)
    for r in rows:
        for col in BD_BLANK_COLUMNS:
            assert r[col] == "", (r["isin"], col)
    assert s.bd_status_counts == {"no_file": len(rows)}
    assert s.bd_conflict_rows == 0
    conn.close()


def test_not_found_file_is_reported_and_leaves_values_blank(tmp_path):
    bd = tmp_path / "bd"
    bd.mkdir()
    (bd / f"{PRICED_LOW_ISIN}.txt").write_text(
        f"Bond with ISIN '{PRICED_LOW_ISIN}' not found.\n\nDisclaimer: synthetic.\n", encoding="utf-8")
    conn, out, _ = _write_bd(tmp_path, bd)
    r = next(x for x in _rows(out / MASTER_REVIEW_NAME) if x["isin"] == PRICED_LOW_ISIN)
    assert r["bd_status"] == "not_found"
    for col in BD_BLANK_COLUMNS:
        assert r[col] == "", col
    md = (out / WORKLIST_MD_NAME).read_text(encoding="utf-8")
    line = next(x for x in md.splitlines() if x.startswith(f"| {PRICED_LOW_ISIN} |"))
    assert "| not found |" in line
    conn.close()


def test_face_and_maturity_conflicts_keep_nse_values_and_are_marked(tmp_path):
    bd = tmp_path / "bd"
    _bd_file(bd, DRAFT_ISIN, face="Rs. 12345.0")            # face conflict, review only
    _bd_file(bd, PRICED_TIE_TOP_ISIN, face="Rs. 12345.0")   # face conflict, in worklist
    _bd_file(bd, PRICED_TIE_ISIN, maturity="2030-05-31")    # maturity conflict, in worklist
    conn, out, s = _write_bd(tmp_path, bd)

    r = next(x for x in _rows(out / MASTER_REVIEW_NAME) if x["isin"] == DRAFT_ISIN)
    assert r["bd_face_vs_nse"] == "conflict"
    assert r["face_hint"] == "100000"            # NSE-name value stays authoritative
    assert r["bd_maturity_vs_nse"] == "agree"

    w = {x["isin"]: x for x in _rows(out / WORKLIST_CSV_NAME)}
    assert w[PRICED_TIE_ISIN]["bd_maturity_vs_nse"] == "conflict"
    assert w[PRICED_TIE_ISIN]["maturity_hint"] == "2031-10-31"
    assert w[PRICED_TIE_TOP_ISIN]["bd_face_vs_nse"] == "conflict"
    assert w[PRICED_TIE_TOP_ISIN]["face_hint"] == "100000"

    md = (out / WORKLIST_MD_NAME).read_text(encoding="utf-8")
    top = next(x for x in md.splitlines() if x.startswith(f"| {PRICED_TIE_TOP_ISIN} |"))
    assert "monthly · secured · A (CRISIL) · CONFLICT: face" in top
    mid = next(x for x in md.splitlines() if x.startswith(f"| {PRICED_TIE_ISIN} |"))
    assert "CONFLICT: maturity" in mid
    assert s.bd_conflict_rows == 3  # DRAFT (face), PRICED_TIE_TOP (face), PRICED_TIE_ISIN (maturity)

    # The BondDekho face and maturity values are never copied into any draft file.
    for name in (DRAFT_MASTER_NAME, MASTER_REVIEW_NAME, WORKLIST_CSV_NAME, WORKLIST_MD_NAME, README_NAME):
        text = (out / name).read_text(encoding="utf-8")
        assert "12345" not in text, name
        assert "2030-05-31" not in text, name
    conn.close()


def test_garbled_frequency_becomes_blank(tmp_path):
    bd = tmp_path / "bd"
    _bd_file(bd, PRICED_LOW_ISIN, freq="Fortnightly-ish")
    conn, out, _ = _write_bd(tmp_path, bd)
    w = next(x for x in _rows(out / WORKLIST_CSV_NAME) if x["isin"] == PRICED_LOW_ISIN)
    assert w["bd_status"] == "found"
    assert w["bd_frequency_unverified"] == ""
    assert w["bd_secured_unverified"] == "secured"
    md = (out / WORKLIST_MD_NAME).read_text(encoding="utf-8")
    line = next(x for x in md.splitlines() if x.startswith(f"| {PRICED_LOW_ISIN} |"))
    assert "Fortnightly" not in line and "| secured · A (CRISIL) |" in line
    conn.close()


def test_master_draft_is_byte_identical_with_and_without_bonddekho(tmp_path):
    bd = tmp_path / "bd"
    for isin in NAMES:
        if isin != ISSUER_ISIN:
            _bd_file(bd, isin, freq="Quarterly", security="Unsecured", face="Rs. 12345.0",
                     maturity="2030-05-31")
    conn = _db(tmp_path)
    plain, with_bd = tmp_path / "plain", tmp_path / "with_bd"
    write_drafts(conn, plain, today=TODAY, max_age_days=7)
    write_drafts(conn, with_bd, today=TODAY, max_age_days=7, bonddekho_dir=bd)
    assert (plain / DRAFT_MASTER_NAME).read_bytes() == (with_bd / DRAFT_MASTER_NAME).read_bytes()
    # Sanity: the review output did change, so the BondDekho path really ran.
    assert (plain / MASTER_REVIEW_NAME).read_bytes() != (with_bd / MASTER_REVIEW_NAME).read_bytes()
    conn.close()


def test_no_network_during_draft_write(tmp_path, monkeypatch):
    bd = tmp_path / "bd"
    _bd_file(bd, DRAFT_ISIN)
    conn = _db(tmp_path)
    _no_network(monkeypatch)
    out = tmp_path / "drafts"
    s = write_drafts(conn, out, today=TODAY, max_age_days=7, bonddekho_dir=bd)
    assert s.bd_status_counts["found"] == 1
    conn.close()


def test_no_bonddekho_leaves_bd_columns_blank_even_with_files(tmp_path, monkeypatch):
    bd = tmp_path / "bd"
    _bd_file(bd, DRAFT_ISIN)
    _bd_file(bd, PRICED_TIE_TOP_ISIN, face="Rs. 12345.0")
    conn = _db(tmp_path)
    _no_network(monkeypatch)
    out = tmp_path / "drafts"
    s = write_drafts(conn, out, today=TODAY, max_age_days=7, bonddekho_dir=None)
    assert s.bd_status_counts == {} and s.bd_conflict_rows == 0
    for name in (MASTER_REVIEW_NAME, WORKLIST_CSV_NAME):
        for r in _rows(out / name):
            for col in BD_COLUMNS:
                assert r[col] == "", (name, r["isin"], col)
    conn.close()


def test_cli_bonddekho_options(tmp_path, monkeypatch, capsys):
    enrich = _load_enrich_script()
    bd = tmp_path / "bd"
    _bd_file(bd, DRAFT_ISIN)
    conn = _db(tmp_path)
    conn.close()
    db_path = str(tmp_path / "bonds.db")
    _no_network(monkeypatch)

    out_on = tmp_path / "on"
    assert enrich.main(["draft", "--db", db_path, "--out", str(out_on), "--bonddekho-dir", str(bd)]) == 0
    assert "found=1 not_found=0 no_file=" in capsys.readouterr().out
    r = next(x for x in _rows(out_on / MASTER_REVIEW_NAME) if x["isin"] == DRAFT_ISIN)
    assert r["bd_status"] == "found" and r["bd_rating_unverified"] == "A"

    out_off = tmp_path / "off"
    assert enrich.main(["draft", "--db", db_path, "--out", str(out_off),
                        "--bonddekho-dir", str(bd), "--no-bonddekho"]) == 0
    assert "disabled (--no-bonddekho)" in capsys.readouterr().out
    r = next(x for x in _rows(out_off / MASTER_REVIEW_NAME) if x["isin"] == DRAFT_ISIN)
    for col in BD_COLUMNS:
        assert r[col] == "", col
    assert (out_on / DRAFT_MASTER_NAME).read_bytes() == (out_off / DRAFT_MASTER_NAME).read_bytes()
