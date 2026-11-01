# india-bonds Phase 1 SPEC (architect: main agent)

Research-only Indian corporate bond screener. Python 3.13, stdlib sqlite3, pandas, streamlit, plotly, requests, pytest.
Integrity rules (apply everywhere): never fabricate ISIN/price/YTM/rating/financials; nullable columns + "unverified" instead of guesses; merge on exact ISIN; instrument rating never replaced by issuer rating; listed != available; every material row carries source_url + fetched_at; vendor YTM stored separately from our computed YTM; missing data lowers confidence.

## Verified data-source facts (Wave 0, spot-checked by main)
- UNIVERSE: `https://nsearchives.nseindia.com/content/equities/List_of_Active_Securities_CM_DEBT.csv` — header `Sr.No.,Company Name,ISIN,Instrument Type`; Instrument Type in {DEBT, Equity, Hybrid}; ~5100 DEBT rows; plain UA, no cookies. NO coupon/maturity/FV/rating.
- PRICES: `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` — header includes TradDt,BizDt,FinInstrmTp,ISIN,TckrSymb,SctySrs,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd. Debt series seen: GB, GS, N0, N1, TB (corporate NCDs = N*/series other than GB/GS/TB/SG; G-sec/T-bill/SGB excluded). Prices for NCDs quoted per face value unit (e.g. 990.00). No coupon/maturity.
- NSE www.nseindia.com root 403s plain curl; nsearchives host works with UA only. Weekend/holiday dates 404 -> treat as "no file", walk back.
- BSE: SPA returns HTTP 200 text/html for ANY path; no verified file URL. BSE = CSV import only. Any downloader MUST verify Content-Type and size, never status alone.
- Ratings agencies: no API/RSS; ratings via `rating_import.csv`. Marketplaces: ToS restrict scraping (WintWealth forbids; GoldenPI/Digifinn restrict copying) -> manual `marketplace_price_import.csv` only. Never scrape marketplaces.
- Coupon/maturity/FV per ISIN are NOT in any verified NSE file -> come from `instrument_master.csv` import (user/issuer-disclosure sourced). Coupon embedded in NSE names (e.g. "ECL 8.88% 2028") may be stored only as `coupon_hint`, never as verified coupon.

## CSV templates (header rows, exact)
instrument_master.csv: `isin,issuer_name,instrument_name,face_value,coupon_pct,coupon_frequency,first_interest_date,maturity_date,issue_date,security_type,seniority,collateral_text,is_perpetual,is_at1,is_structured,default_flag,trading_suspended,currency,source_url,retrieved_on`
 - coupon_frequency in {monthly,quarterly,semiannual,annual,zero,at_maturity}; security_type in {secured,unsecured,unknown}; seniority in {senior,subordinated,unknown}; booleans `true/false/` (blank=unknown); dates ISO YYYY-MM-DD.
rating_import.csv: `isin,issuer_name,instrument_name,instrument_type,rating_agency,rating,outlook,rating_action,action_date,rationale_url,source_type,source_ref,retrieved_on` (rating_action in assigned/reaffirmed/upgraded/downgraded/withdrawn; source_type in nse_filing/bse_filing/agency_site/manual)
marketplace_price_import.csv: `isin,platform,instrument_name,issuer_name,face_value,price,ytm_pct,coupon_pct,coupon_frequency,maturity_date,min_investment,availability,security_type,collateral_text,observed_at,source_url,notes` (availability in open/closed/sold_out/unknown; `price` = clean price per face_value; `ytm_pct` = vendor YTM, stored as vendor_ytm only)

## SQLite schema (database/schema.sql) — tables
source_record(id PK, source TEXT, url TEXT, fetched_at TEXT ISO-UTC, content_type TEXT, size_bytes INT, sha256 TEXT, raw_path TEXT, status TEXT {ok,unavailable,error}, note TEXT)
issuer(id PK, name UNIQUE, sector TEXT NULL, group_name TEXT NULL)
instrument(isin PK, issuer_id FK NULL, name, exchange_listed INT, instrument_type TEXT, face_value REAL NULL, coupon_pct REAL NULL, coupon_frequency TEXT NULL, first_interest_date, maturity_date, issue_date, security_type, seniority, collateral_text, is_perpetual INT NULL, is_at1 INT NULL, is_structured INT NULL, default_flag INT NULL, trading_suspended INT NULL, currency TEXT NULL, coupon_hint REAL NULL, master_source_id FK NULL)
rating(id PK, isin FK, agency, rating, outlook, action, action_date, rationale_url, source_id FK, UNIQUE(isin,agency,action_date,rating))
price_snapshot(id PK, isin FK, trade_date, clean_price REAL, volume REAL, value REAL, trades INT, kind TEXT {exchange_close,marketplace}, platform TEXT NULL, vendor_ytm REAL NULL, freshness TEXT {live,delayed,stale,indicative}, source_id FK, observed_at)
availability(id PK, isin FK, listed INT, recently_traded INT, offered_for_sale INT, price_verified INT, executable_verified INT, min_investment REAL NULL, platform TEXT NULL, observed_at, source_id FK)
rejection(id PK, run_id TEXT, isin, rule TEXT, reason TEXT, created_at)
screen_result(id PK, run_id, isin, bucket TEXT {primary,speculative,rejected}, ytm REAL NULL, dirty_price REAL NULL, discount_pct REAL NULL, next_interest_date, confidence TEXT {High,Medium,Low}, created_at)
All tables append-only for snapshots (price_snapshot, rating, availability, source_record); instrument is upserted.

## analytics API (pure functions, no I/O) — analytics/cashflows.py, analytics/ytm.py
```python
@dataclass(frozen=True) class CashFlow: date: date; amount: float; kind: str  # 'coupon'|'principal'
def build_schedule(face_value: float, coupon_pct: float, frequency: str, maturity: date,
                   first_interest_date: date|None=None, issue_date: date|None=None) -> list[CashFlow]
    # dates generated BACKWARD from maturity by months (1,3,6,12) with end-of-month clamping; if first_interest_date given it must be consistent else raise ValueError; 'zero'/'at_maturity' -> single flow. Coupon per period = face*coupon_pct/100/periods_per_year. Final flow = coupon + principal as two CashFlows on maturity date. NO business-day adjustment in Phase 1 (documented limitation).
def future_flows(schedule, settlement: date) -> list[CashFlow]   # flows with date > settlement
def accrued_interest(schedule_params..., settlement: date, daycount='30/360') -> float  # from previous coupon date (or issue/first) to settlement; 0 for zero-coupon
def next_interest_date(schedule, settlement) -> date|None
def xirr_yield(flows: list[CashFlow], settlement: date, dirty_price: float, txn_cost_pct: float=0.0) -> float|None
    # effective annual yield, Act/365: sum(amount_i / (1+y)^((d_i-settlement)/365)) = dirty*(1+txn_cost_pct/100); Newton w/ bisection fallback on [-0.99, 10]; None if no root/no future flows; never raises on bad input except ValueError for price<=0
def compute_ytm(face, coupon_pct, frequency, maturity, settlement, clean_price_per_face_unit, ...) -> YtmResult(ytm: float|None, dirty: float, accrued: float, discount_pct: float, flags: list[str])  # flags include 'below_98','below_95','below_90','premium'
```
Prices are given per face value as quoted (clean_price in same units as face_value, e.g. face 1000, price 990). discount_pct = (face-clean)/face*100.

## Hand-calc test anchors (tests must include; main re-derives independently)
1. Zero-coupon, face 100, price 80, settlement 2027-01-01, maturity 2029-01-01 (731 days incl. 2028 leap): y = (100/80)^(365/731)-1 ≈ 0.11787 (tolerance 1e-4; compute exactly in test).
2. Zero-coupon exactly 730 days: y=(1.25)^(0.5)-1=0.118034.
3. Par bond annual 10%, price 100, settlement on coupon date, 3y -> yield within 1e-3 of 0.10 (leap-day drift).
4. Quarterly 9% face 1000 clean 1000 mid-period: accrued = 1000*0.09*(days30/360) hand-checked; dirty=clean+accrued.
5. Discount flags: clean 940/1000 -> flags {'below_98','below_95'} (not below_90); 880 -> all three.
6. Monthly payout schedule length = months to maturity; dates are real calendar dates (not annual/12 amounts: each coupon = face*c/100/12).
7. Edge: settlement >= maturity -> ytm None; price<=0 -> ValueError.

## eligibility API — screening/eligibility.py
`evaluate(conn, isin, settlement, run_id) -> EligibilityResult(eligible: bool, failures: list[(rule, reason)], bucket)`. Rules (rule ids): `no_active_isin`, `not_inr`, `maturity_over_10y`, `matured`, `missing_coupon_or_schedule`, `no_instrument_rating`, `unrated_only_issuer_rating` (never used as pass), `perpetual`, `at1`, `structured`, `default_flag`, `trading_suspended`, `stale_price` (>7 days since last exchange_close/marketplace price) -> reject as not price-verified, `rating_conflict` -> flag (not reject) and lowers confidence. Unknown (NULL) booleans are NOT assumed safe: `is_perpetual/is_at1/is_structured/default_flag/trading_suspended` NULL -> failure `unverified_<field>` for primary, record reason. Bucket: primary if worst-agency instrument rating in AAA..AA-, speculative if A+/A, else rejected (BBB and below, rule `rating_below_A`). Rating = most recent action per agency, non-withdrawn; take the LOWEST across agencies as conservative. Confidence: High = master fields complete + rating + price within 7d + availability.executable_verified; Medium = missing one of price-verified/availability; Low otherwise.

## Connector API — data_sources/
`base.Connector`: `fetch(self) -> SourceRecord` saves bytes to `data/raw/<source>/<UTC ts>_<name>`, writes source_record row (status ok/unavailable/error), validates Content-Type & min size (reject text/html when csv/zip expected), UA header, >=1s between requests, 1 retry max, never raises on network failure (returns status 'unavailable'). `nse.NseDebtUniverse` (active list), `nse.NseCmBhavcopy(date)` (parse series filter, walk back up to 5 days on 404). `bse.py`: stub class that returns status 'unavailable' with note "no verified BSE file URL; use CSV import". `csv_import.py`: `import_instrument_master(conn, path)`, `import_ratings(conn, path)`, `import_marketplace(conn, path)` — validate headers exactly, reject rows with malformed ISIN (regex `^IN[A-Z0-9]{10}$`), report row-level errors, record source_record, never default missing values.

## File ownership (disjoint)
B1: india_bonds/analytics/*, tests/test_cashflows.py, tests/test_ytm.py
B2: india_bonds/database/*, india_bonds/data_sources/csv_import.py, tests/test_db.py, tests/test_csv_import.py
B3: india_bonds/data_sources/{__init__,base,nse,bse}.py, tests/test_connectors.py (with mocked HTTP; plus one optional live smoke script scripts/smoke_connectors.py)
B4: india_bonds/screening/*, tests/test_eligibility.py
B5: india_bonds/dashboard/app.py
Package root: ./india_bonds ; pyproject.toml at repo root (main creates).
