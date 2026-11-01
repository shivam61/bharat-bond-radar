# bharat-bond-radar

Research-only screener for Indian corporate bonds and NCDs. It fetches public exchange data, screens bonds against eligibility rules, computes an independent YTM, and writes a day-wise Markdown report. **Not investment advice. It never executes transactions.**

**Phase 1 status:** eligibility screening only. There is no scoring, repayment-capacity analysis or risk grade yet, and nothing here is a buy/sell recommendation.

## Run it
```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python scripts/refresh.py --init-inputs   # creates blank CSV templates in data/inputs/
.venv/bin/python scripts/refresh.py                 # fetch -> update DB (only if changed) -> screen -> report
.venv/bin/python -m pytest -q
```
Reports are written to `reports/<date>/` (`README.md`, one page per shortlisted bond, `rejected.md`, CSVs).

## Data
- **Automatic:** the NSE active-debt list, the NSE daily bhavcopy, and NSE's public corporate-bond trade feed (OTC/RFQ).
- **You supply (CSV):** `instrument_master.csv` (coupon, maturity, flags), `ratings.csv` (instrument-level ratings) and `marketplace.csv`. NSE files do not carry these, so until they are provided every bond is rejected for missing data. The BSE has no verified downloadable file yet.
- Nothing is invented: missing data lowers confidence or rejects the bond. See `SPEC.md` for the rules and schema.

## Data-use note
NSE data is subject to NSE's data policy and terms of use. This project is intended for personal research. Check NSE's terms before redistributing any data.
