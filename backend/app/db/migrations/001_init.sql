-- TimescaleDB schema — applied automatically by Docker entrypoint on first run
-- The app also calls init_db() on startup which is idempotent.

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

CREATE TABLE IF NOT EXISTS price_ticks (
    ts          TIMESTAMPTZ      NOT NULL,
    exchange    TEXT             NOT NULL,
    symbol      TEXT             NOT NULL,
    bid         DOUBLE PRECISION,
    ask         DOUBLE PRECISION,
    last        DOUBLE PRECISION
);

SELECT create_hypertable('price_ticks', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_price_ticks_sym_ex ON price_ticks (symbol, exchange, ts DESC);

CREATE TABLE IF NOT EXISTS arbitrage_alerts (
    ts              TIMESTAMPTZ      NOT NULL,
    symbol          TEXT             NOT NULL,
    buy_exchange    TEXT             NOT NULL,
    sell_exchange   TEXT             NOT NULL,
    buy_price       DOUBLE PRECISION,
    sell_price      DOUBLE PRECISION,
    spread_pct      DOUBLE PRECISION
);

SELECT create_hypertable('arbitrage_alerts', 'ts', if_not_exists => TRUE);

-- Retention: 7 days of raw ticks (continuous aggregate retains its own copy)
SELECT add_retention_policy('price_ticks',      INTERVAL '7 days',  if_not_exists => TRUE);
-- Retention: 30 days of arbitrage alerts
SELECT add_retention_policy('arbitrage_alerts', INTERVAL '30 days', if_not_exists => TRUE);

-- Instruments registry — must exist before 002_seed_instruments.sql runs
CREATE TABLE IF NOT EXISTS instruments (
    id          SERIAL      PRIMARY KEY,
    canonical   TEXT        NOT NULL UNIQUE,
    type        TEXT        NOT NULL DEFAULT 'spot',
    base_asset  TEXT        NOT NULL DEFAULT '',
    quote_asset TEXT        NOT NULL DEFAULT '',
    description TEXT        NOT NULL DEFAULT '',
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    aliases     JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Daily OHLCV (historical backfill) — also referenced by the backend on startup
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ts           TIMESTAMPTZ      NOT NULL,
    symbol       TEXT             NOT NULL,
    exchange     TEXT             NOT NULL,
    open         DOUBLE PRECISION NOT NULL DEFAULT 0,
    high         DOUBLE PRECISION NOT NULL DEFAULT 0,
    low          DOUBLE PRECISION NOT NULL DEFAULT 0,
    close        DOUBLE PRECISION NOT NULL DEFAULT 0,
    base_volume  DOUBLE PRECISION NOT NULL DEFAULT 0,
    quote_volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE (ts, symbol, exchange)
);

SELECT create_hypertable('ohlcv_daily', 'ts', if_not_exists => TRUE);

-- Continuous aggregate: 1-minute OHLCV
CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket,
       exchange,
       symbol,
       first(last, ts)  AS open,
       max(last)        AS high,
       min(last)        AS low,
       last(last, ts)   AS close,
       count(*)         AS ticks
FROM price_ticks
GROUP BY bucket, exchange, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_1m',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE
);

-- Retention: keep 7 days of 1-minute aggregates (matches raw tick retention)
SELECT add_retention_policy('ohlcv_1m', INTERVAL '7 days', if_not_exists => TRUE);

-- Funding rates (settled, used for backtesting) — 2 years
CREATE TABLE IF NOT EXISTS funding_rates (
    time           TIMESTAMPTZ      NOT NULL,
    symbol         TEXT             NOT NULL,
    exchange       TEXT             NOT NULL,
    rate           DOUBLE PRECISION NOT NULL,
    interval_hours SMALLINT         NOT NULL DEFAULT 8,
    UNIQUE (time, symbol, exchange)
);

SELECT create_hypertable('funding_rates', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_funding_rates_sym_ex
    ON funding_rates (symbol, exchange, time DESC);

SELECT add_retention_policy('funding_rates', INTERVAL '2 years', if_not_exists => TRUE);
