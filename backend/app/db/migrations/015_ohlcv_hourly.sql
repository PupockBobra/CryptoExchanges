-- Hourly OHLCV for crypto exchanges (Hourly Volume page, 31.07.2026).
--
-- Same shape as ohlcv_daily but one row per hour.  Kept separate rather than
-- widening ohlcv_daily with a granularity column: every existing volume query
-- reads ohlcv_daily unqualified, and a mixed-granularity table would double-count
-- in all of them.
--
-- Retention is 90 days — the page shows at most a 30-day profile, and hourly rows
-- accumulate ~24x faster than daily ones.  No compression policy: the ETL upserts
-- the most recent hours on every pass, and compressed chunks make that a
-- decompress-rewrite cycle for no meaningful space win at this row count.

CREATE TABLE IF NOT EXISTS ohlcv_hourly (
    ts           TIMESTAMPTZ      NOT NULL,
    symbol       TEXT             NOT NULL,
    exchange     TEXT             NOT NULL,
    open         DOUBLE PRECISION NOT NULL DEFAULT 0,
    high         DOUBLE PRECISION NOT NULL DEFAULT 0,
    low          DOUBLE PRECISION NOT NULL DEFAULT 0,
    close        DOUBLE PRECISION NOT NULL DEFAULT 0,
    base_volume  DOUBLE PRECISION NOT NULL DEFAULT 0,
    quote_volume DOUBLE PRECISION NOT NULL DEFAULT 0
);

SELECT create_hypertable('ohlcv_hourly', 'ts', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS ohlcv_hourly_uniq
    ON ohlcv_hourly (ts, symbol, exchange);

SELECT add_retention_policy('ohlcv_hourly', INTERVAL '90 days', if_not_exists => TRUE);
