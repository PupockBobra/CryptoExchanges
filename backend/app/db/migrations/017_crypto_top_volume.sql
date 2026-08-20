-- Top-100 crypto perps per exchange — turnover for the Cryptocurrencies slice of
-- the asset-group charts on TradFi Market Share (03.08.2026).
--
-- Until now that slice was the three curated majors (BTC/ETH/SOL) from
-- `instruments`, while the Commodities and US Market slices covered their whole
-- universes — so crypto was structurally understated on every share chart.
--
-- Kept apart from ohlcv_daily/ohlcv_hourly for the same reason the stock tables
-- are: those feed per-instrument pages that render a card per symbol, and 600
-- extra symbols would flood them.  `symbol` here is the canonical BASE ticker
-- (BTC, ETH, DOGE …) so one coin aggregates across venues.
--
--   quote_usd = close × volume × contractSize  (contractSize matters on MEXC)
-- USD→RUB happens at query time via moex_fx_rates (USDRUBF, forward-filled).

CREATE TABLE IF NOT EXISTS crypto_top_daily_volume (
    date      DATE           NOT NULL,
    exchange  TEXT           NOT NULL,
    symbol    TEXT           NOT NULL,
    quote_usd NUMERIC(24, 2) NOT NULL,
    PRIMARY KEY (date, exchange, symbol)
);

CREATE INDEX IF NOT EXISTS idx_crypto_top_daily_date
    ON crypto_top_daily_volume (date DESC);

-- Hourly counterpart for the intraday profile.  Retention 45 days (the profile
-- averages over 30); no compression, the ETL rewrites the newest hours hourly.
CREATE TABLE IF NOT EXISTS crypto_top_hourly_volume (
    hour      TIMESTAMPTZ    NOT NULL,
    exchange  TEXT           NOT NULL,
    symbol    TEXT           NOT NULL,
    quote_usd NUMERIC(24, 2) NOT NULL
);

SELECT create_hypertable('crypto_top_hourly_volume', 'hour', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS crypto_top_hourly_uniq
    ON crypto_top_hourly_volume (hour, exchange, symbol);

SELECT add_retention_policy('crypto_top_hourly_volume', INTERVAL '45 days', if_not_exists => TRUE);
