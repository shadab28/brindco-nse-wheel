-- NSE EOD bhavcopy warehouse.  Requires PostgreSQL 12+ (declarative partitioning
-- with ON CONFLICT).  Safe to re-run: every object is created IF NOT EXISTS.

CREATE SCHEMA IF NOT EXISTS nse;

-- ---------------------------------------------------------------- cash segment
-- One row per (trading day, symbol).  `close` here is the official NSE close.
CREATE TABLE IF NOT EXISTS nse.cash_eod (
    trade_date      date            NOT NULL,
    symbol          text            NOT NULL,   -- canonical (post-rename) ticker
    source_symbol   text            NOT NULL,   -- ticker as printed in the bhavcopy
    series          text            NOT NULL,
    open            numeric(18,4),
    high            numeric(18,4),
    low             numeric(18,4),
    close           numeric(18,4),
    last            numeric(18,4),
    prev_close      numeric(18,4),
    volume          bigint,
    turnover        numeric(22,4),
    trades          bigint,
    isin            text,
    PRIMARY KEY (trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS cash_eod_symbol_date_idx
    ON nse.cash_eod (symbol, trade_date);

-- --------------------------------------------------------------------- futures
CREATE TABLE IF NOT EXISTS nse.futures_eod (
    trade_date      date            NOT NULL,
    symbol          text            NOT NULL,
    source_symbol   text            NOT NULL,
    expiry          date            NOT NULL,
    instrument      text            NOT NULL,   -- FUTSTK/FUTIDX (legacy) or STF/IDF (UDiFF)
    open            numeric(18,4),
    high            numeric(18,4),
    low             numeric(18,4),
    close           numeric(18,4),
    settle          numeric(18,4),
    contracts       bigint,
    value_lakh      numeric(22,4),
    open_interest   bigint,
    change_in_oi    bigint,
    dte             integer,
    PRIMARY KEY (trade_date, symbol, expiry)
);
CREATE INDEX IF NOT EXISTS futures_eod_symbol_expiry_idx
    ON nse.futures_eod (symbol, expiry, trade_date);

-- --------------------------------------------------------------------- options
-- Partitioned by year: ~20M rows for the NIFTY 50 over 2019-2026, and every
-- backtest query is date-bounded, so range pruning pays for itself.
CREATE TABLE IF NOT EXISTS nse.options_eod (
    trade_date      date            NOT NULL,
    symbol          text            NOT NULL,
    source_symbol   text            NOT NULL,
    expiry          date            NOT NULL,
    strike          numeric(18,4)   NOT NULL,
    option_type     text            NOT NULL,   -- 'CE' | 'PE'
    instrument      text            NOT NULL,   -- OPTSTK/OPTIDX or STO/IDO
    open            numeric(18,4),
    high            numeric(18,4),
    low             numeric(18,4),
    close           numeric(18,4),   -- STALE for untraded strikes: prefer `settle`
    settle          numeric(18,4),   -- exchange settlement price; use for marking
    contracts       bigint,          -- 0 => the close above is a carry-forward
    value_lakh      numeric(22,4),
    open_interest   bigint,
    change_in_oi    bigint,
    dte             integer,
    PRIMARY KEY (trade_date, symbol, expiry, strike, option_type)
) PARTITION BY RANGE (trade_date);

DO $$
DECLARE y int;
BEGIN
    FOR y IN 2016..2027 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS nse.options_eod_%s '
            'PARTITION OF nse.options_eod FOR VALUES FROM (%L) TO (%L)',
            y, make_date(y,1,1), make_date(y+1,1,1));
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS options_eod_symbol_expiry_idx
    ON nse.options_eod (symbol, expiry, strike, trade_date);
CREATE INDEX IF NOT EXISTS options_eod_symbol_date_idx
    ON nse.options_eod (symbol, trade_date);

-- ------------------------------------------------------------------ ingest log
-- Lets a re-run skip days already loaded, and records holidays so we do not
-- re-request a file NSE will never have.
CREATE TABLE IF NOT EXISTS nse.ingest_log (
    trade_date      date            NOT NULL,
    segment         text            NOT NULL,   -- 'cm' | 'fo'
    status          text            NOT NULL,   -- 'ok' | 'no_file' | 'error'
    row_count       integer,
    message         text,
    universe_hash   text,                       -- invalidates when symbols change
    ingested_at     timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, segment)
);

-- ------------------------------------------------------------------ convenience
CREATE OR REPLACE VIEW nse.coverage AS
SELECT symbol,
       min(trade_date) AS first_date,
       max(trade_date) AS last_date,
       count(*)        AS trading_days
FROM nse.cash_eod
GROUP BY symbol
ORDER BY symbol;

-- ====================================================================
-- Point-in-time universe selection (avoids survivorship bias from using
-- today's index membership).  Populated by build_universe.py.
-- ====================================================================

-- Daily per-symbol F&O activity for EVERY F&O stock, not just a chosen few.
-- Deliberately an aggregate: ranking needs turnover and OI, not every strike,
-- so the whole pool fits in ~400k rows instead of ~88M.
CREATE TABLE IF NOT EXISTS nse.fo_liquidity_daily (
    trade_date       date     NOT NULL,
    symbol           text     NOT NULL,
    opt_turnover_cr  numeric(20,4),   -- stock-option notional traded, INR crore
    opt_contracts    bigint,
    opt_oi           bigint,
    n_strikes        integer,         -- distinct strikes quoted (chain breadth)
    n_traded_strikes integer,         -- strikes with contracts > 0 (real liquidity)
    fut_turnover_cr  numeric(20,4),
    fut_oi           bigint,
    PRIMARY KEY (trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS fo_liquidity_symbol_idx
    ON nse.fo_liquidity_daily (symbol, trade_date);

-- The selected universe, one row per (rebalance period, symbol).
CREATE TABLE IF NOT EXISTS nse.universe_membership (
    rebalance_date   date     NOT NULL,
    effective_from   date     NOT NULL,
    effective_to     date     NOT NULL,
    symbol           text     NOT NULL,
    rank             integer  NOT NULL,
    opt_turnover_cr  numeric(20,4),
    opt_oi           bigint,
    traded_strike_frac numeric(8,4),
    ruleset          text     NOT NULL,   -- which filter produced this
    PRIMARY KEY (rebalance_date, symbol, ruleset)
);
CREATE INDEX IF NOT EXISTS universe_effective_idx
    ON nse.universe_membership (ruleset, effective_from, effective_to);

-- ====================================================================
-- Monthly expiry cycles for the wheel backtest (build_cycles.py)
-- ====================================================================

-- Derived from the data, not from a "last Thursday" rule: over 2019-2026 the
-- observed stock-option expiries fall on 69 Thursdays, 13 Tuesdays (NSE moved
-- the expiry day in 2025), 3 Wednesdays and 1 Monday (holiday shifts).
--
-- `expiry_date` is the last date the series actually traded. The bhavcopy can
-- carry a stale stamp for a series whose expiry was advanced for a holiday --
-- the Jun 2023 series quoted 2023-06-29 for its whole life and settled on
-- 2023-06-28 -- so `quoted_expiry_stamps` keeps every stamp seen, and the
-- backtest must join contracts on contract_month, not on the raw stamp.
CREATE TABLE IF NOT EXISTS nse.expiry_calendar (
    contract_month        date    NOT NULL,   -- first of the expiry month
    expiry_date           date    NOT NULL,   -- true last trading day of series
    quoted_expiry_stamps  date[]  NOT NULL,
    prev_expiry_date      date,
    cycle_start_date      date    NOT NULL,   -- first trading day of the cycle
    cycle_trading_days    integer,
    n_symbols             integer,            -- stocks with a chain that month
    PRIMARY KEY (contract_month)
);
CREATE INDEX IF NOT EXISTS expiry_calendar_expiry_idx
    ON nse.expiry_calendar (expiry_date);

-- Point-in-time tradeable universe, one row per (cycle, eligible stock).
-- This is what the wheel backtest iterates over.
CREATE TABLE IF NOT EXISTS nse.cycle_universe (
    contract_month     date     NOT NULL,
    cycle_start_date   date     NOT NULL,
    expiry_date        date     NOT NULL,
    symbol             text     NOT NULL,
    instrument_token   text,               -- exchange/broker token where available
    constituent_source text     NOT NULL,  -- provenance of the membership claim
    has_option_chain   boolean  NOT NULL,  -- chain present in nse.options_eod
    n_strikes          integer,
    lot_size           integer,
    PRIMARY KEY (contract_month, symbol, constituent_source)
);
CREATE INDEX IF NOT EXISTS cycle_universe_cycle_idx
    ON nse.cycle_universe (cycle_start_date, expiry_date);

-- Per contract-series lot size and instrument token (build_cycles.py lots).
-- Lot size drives position sizing, and NSE revises lots per series, so this is
-- keyed by (symbol, expiry) rather than by symbol.
CREATE TABLE IF NOT EXISTS nse.contract_lots (
    symbol            text    NOT NULL,
    expiry            date    NOT NULL,
    contract_month    date    NOT NULL,
    lot_size          integer,
    lot_source        text,              -- 'NewBrdLotQty' (declared) | 'oi_gcd' (derived)
    lot_from_oi       integer,
    declared_lot      integer,
    instrument_token  text,              -- UDiFF FinInstrmId; NULL before 2024-07
    n_obs             integer,
    first_seen        date,
    last_seen         date,
    PRIMARY KEY (symbol, expiry)
);
CREATE INDEX IF NOT EXISTS contract_lots_month_idx
    ON nse.contract_lots (contract_month, symbol);

-- Step 6-7: the backtest-ready front-month chain. One row per option contract
-- per trading day, restricted to (a) stocks eligible in that cycle under a
-- given constituent_source, and (b) the cycle window [cycle_start, expiry].
-- Carries spot and lot size so the wheel's strike selection and sizing need no
-- further joins.
CREATE TABLE IF NOT EXISTS nse.wheel_chain (
    constituent_source text     NOT NULL,
    contract_month     date     NOT NULL,
    cycle_start_date   date     NOT NULL,
    expiry_date        date     NOT NULL,
    symbol             text     NOT NULL,
    trade_date         date     NOT NULL,
    dte                integer  NOT NULL,   -- trading-calendar days to expiry
    strike             numeric(18,4) NOT NULL,
    option_type        text     NOT NULL,
    close              numeric(18,4),
    settle             numeric(18,4),       -- mark here, NOT on close
    contracts          bigint,
    open_interest      bigint,
    lot_size           integer,
    spot_close         numeric(18,4),       -- underlying EQ close, same day
    moneyness          numeric(12,6),       -- strike / spot
    PRIMARY KEY (constituent_source, contract_month, symbol, trade_date,
                 strike, option_type)
);
CREATE INDEX IF NOT EXISTS wheel_chain_cycle_idx
    ON nse.wheel_chain (constituent_source, cycle_start_date, symbol);
CREATE INDEX IF NOT EXISTS wheel_chain_symbol_date_idx
    ON nse.wheel_chain (symbol, trade_date);
