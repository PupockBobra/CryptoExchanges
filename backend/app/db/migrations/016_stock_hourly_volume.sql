-- Equity (stock) perpetual-futures HOURLY turnover on crypto exchanges
-- (TradFi Market Share → Hourly, 03.08.2026).
--
-- Same content as stock_daily_volume but one row per hour, so the hour-of-day
-- profile can cover the whole equity-perp universe (~520 pairs / ~200 tickers)
-- instead of only the handful of curated names in ohlcv_hourly.
--
-- Kept out of ohlcv_hourly deliberately: that table drives the Hourly Volume
-- page, which renders one chart card per symbol — 200 extra tickers would turn
-- it into 200 cards and blow up its payload.
--
--   quote_usd = close × volume × contractSize   (contractSize matters on MEXC)
-- USD→RUB happens at query time via moex_fx_rates (USDRUBF, forward-filled).
--
-- Retention 45 days: the page averages over a 30-day window, and hourly rows
-- accumulate ~24x faster than daily ones.  No compression — the ETL upserts the
-- most recent hours every pass, which on compressed chunks is a
-- decompress-rewrite cycle for no meaningful space win.

CREATE TABLE IF NOT EXISTS stock_hourly_volume (
    hour      TIMESTAMPTZ    NOT NULL,
    exchange  TEXT           NOT NULL,
    ticker    TEXT           NOT NULL,
    quote_usd NUMERIC(24, 2) NOT NULL
);

SELECT create_hypertable('stock_hourly_volume', 'hour', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS stock_hourly_volume_uniq
    ON stock_hourly_volume (hour, exchange, ticker);

SELECT add_retention_policy('stock_hourly_volume', INTERVAL '45 days', if_not_exists => TRUE);
