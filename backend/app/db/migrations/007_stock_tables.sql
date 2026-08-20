-- Equity (stock) perpetual-futures daily turnover on crypto exchanges
-- (Binance, OKX, Bybit, MEXC, Hyperliquid).
--
-- Per (date, exchange, canonical ticker) the daily turnover in USD is stored:
--   quote_usd = close × volume × contractSize
-- (contractSize matters only on MEXC; it is 1 elsewhere).
-- USD→RUB happens at query time via moex_fx_rates (USDRUBF, forward-filled),
-- the same conversion used by every other volume page so charts compare.

CREATE TABLE IF NOT EXISTS stock_daily_volume (
    date      DATE           NOT NULL,
    exchange  TEXT           NOT NULL,
    ticker    TEXT           NOT NULL,
    quote_usd NUMERIC(24, 2) NOT NULL,
    PRIMARY KEY (date, exchange, ticker)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_volume_date
    ON stock_daily_volume (date DESC);
