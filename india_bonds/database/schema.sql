-- india-bonds Phase 1 schema (SQLite). Per SPEC.md.
-- Snapshot tables are append-only; instrument is upserted by isin.

CREATE TABLE IF NOT EXISTS source_record (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    url           TEXT,
    fetched_at    TEXT NOT NULL,               -- ISO-8601 UTC
    content_type  TEXT,
    size_bytes    INTEGER,
    sha256        TEXT,
    raw_path      TEXT,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'unavailable', 'error')),
    note          TEXT
);

CREATE TABLE IF NOT EXISTS issuer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    sector      TEXT,
    group_name  TEXT
);

CREATE TABLE IF NOT EXISTS instrument (
    isin                 TEXT PRIMARY KEY,
    issuer_id            INTEGER REFERENCES issuer(id),
    name                 TEXT,
    exchange_listed      INTEGER CHECK (exchange_listed IN (0, 1)),
    instrument_type      TEXT,
    face_value           REAL,
    coupon_pct           REAL,
    coupon_frequency     TEXT CHECK (coupon_frequency IN
                             ('monthly', 'quarterly', 'semiannual', 'annual', 'zero', 'at_maturity')),
    first_interest_date  TEXT,
    maturity_date        TEXT,
    issue_date           TEXT,
    security_type        TEXT CHECK (security_type IN ('secured', 'unsecured', 'unknown')),
    seniority            TEXT CHECK (seniority IN ('senior', 'subordinated', 'unknown')),
    collateral_text      TEXT,
    is_perpetual         INTEGER CHECK (is_perpetual IN (0, 1)),
    is_at1               INTEGER CHECK (is_at1 IN (0, 1)),
    is_structured        INTEGER CHECK (is_structured IN (0, 1)),
    default_flag         INTEGER CHECK (default_flag IN (0, 1)),
    trading_suspended    INTEGER CHECK (trading_suspended IN (0, 1)),
    currency             TEXT,
    coupon_hint          REAL,
    master_source_id     INTEGER REFERENCES source_record(id),
    source_url           TEXT,
    retrieved_on         TEXT
);

CREATE TABLE IF NOT EXISTS rating (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    isin          TEXT NOT NULL REFERENCES instrument(isin),
    agency        TEXT NOT NULL,
    rating        TEXT NOT NULL,
    outlook       TEXT,
    action        TEXT CHECK (action IN ('assigned', 'reaffirmed', 'upgraded', 'downgraded', 'withdrawn')),
    action_date   TEXT,
    rationale_url TEXT,
    source_id     INTEGER REFERENCES source_record(id),
    source_type   TEXT CHECK (source_type IN ('nse_filing', 'bse_filing', 'agency_site', 'manual')),
    source_ref    TEXT,
    UNIQUE (isin, agency, action_date, rating)
);
CREATE INDEX IF NOT EXISTS ix_rating_isin ON rating(isin);

CREATE TABLE IF NOT EXISTS price_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    isin        TEXT NOT NULL REFERENCES instrument(isin),
    trade_date  TEXT,
    clean_price REAL,
    volume      REAL,
    value       REAL,
    trades      INTEGER,
    kind        TEXT NOT NULL CHECK (kind IN ('exchange_close', 'marketplace', 'exchange_reported')),
    platform    TEXT,
    vendor_ytm  REAL,                          -- vendor-quoted YTM; never our computed YTM
    freshness   TEXT CHECK (freshness IN ('live', 'delayed', 'stale', 'indicative')),
    source_id   INTEGER REFERENCES source_record(id),
    observed_at TEXT,
    source_url  TEXT,
    notes       TEXT,
    -- exchange_reported only (NSE OTC/RFQ 'liveCorp-bonds'): price as reported per 100 face
    -- (basis unverified) and the traded yield as reported by NSE (percent, unverified).
    price_per_100      REAL,
    reported_yield_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_price_snapshot_isin ON price_snapshot(isin, kind, observed_at);

CREATE TABLE IF NOT EXISTS availability (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    isin                TEXT NOT NULL REFERENCES instrument(isin),
    listed              INTEGER CHECK (listed IN (0, 1)),
    recently_traded     INTEGER CHECK (recently_traded IN (0, 1)),
    offered_for_sale    INTEGER CHECK (offered_for_sale IN (0, 1)),
    price_verified      INTEGER CHECK (price_verified IN (0, 1)),
    executable_verified INTEGER CHECK (executable_verified IN (0, 1)),
    min_investment      REAL,
    platform            TEXT,
    observed_at         TEXT,
    source_id           INTEGER REFERENCES source_record(id),
    source_url          TEXT
);
CREATE INDEX IF NOT EXISTS ix_availability_isin ON availability(isin);

CREATE TABLE IF NOT EXISTS rejection (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT,
    isin       TEXT,
    rule       TEXT,
    reason     TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS screen_result (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT,
    isin              TEXT,
    bucket            TEXT CHECK (bucket IN ('primary', 'speculative', 'rejected')),
    ytm               REAL,
    dirty_price       REAL,
    discount_pct      REAL,
    next_interest_date TEXT,
    confidence        TEXT CHECK (confidence IN ('High', 'Medium', 'Low')),
    created_at        TEXT,
    worst_rating      TEXT,   -- lowest instrument rating across agencies (never issuer rating)
    flags             TEXT,   -- comma-separated: rating_conflict, credit_enhanced, agency_withdrawn, ...
    clean_price       REAL,
    price_date        TEXT,
    price_kind        TEXT,
    settlement        TEXT,   -- screening date used for this run (YYYY-MM-DD)
    vendor_ytm        REAL    -- vendor-quoted YTM, shown for comparison only; never used as our YTM
);

-- DRAFT, UNVERIFIED hints parsed from NSE descriptor names (instrument.name). Never master data:
-- nothing in the screener reads this table. Written by refresh (descriptor hints step) only when
-- the parsed values change; observed_at is when the current value was first seen.
CREATE TABLE IF NOT EXISTS descriptor_hint (
    isin           TEXT PRIMARY KEY REFERENCES instrument(isin),
    raw            TEXT,
    issuer         TEXT,
    series         TEXT,
    kind           TEXT,
    coupon_pct     REAL,
    maturity_date  TEXT,
    face_value     REAL,
    perpetual_hint INT,
    at1_hint       INT,
    call_hint      INT,
    warnings       TEXT,                       -- '; '-joined parser warnings
    source_id      INTEGER REFERENCES source_record(id),
    observed_at    TEXT                        -- ISO-8601 UTC
);
