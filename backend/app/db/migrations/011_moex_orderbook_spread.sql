-- MOEX crypto-index futures "spread on volume" (AVG_SPREAD) history.
--
-- Same methodology as spb_orderbook_spread, but for the 5 MOEX FORTS crypto
-- futures (BTC/ETH/SOL/XRP/TRX), fetched via Finam under MIC RTSX.  Overlaid on
-- the matching SPB crypto Order Book card, so `ticker` is the SPB crypto ticker
-- it maps to (e.g. 'BTCUSDperpA').  spread_1m_usd is the per-contract USD spread
-- to trade 1 млн ₽ per side (walking the book); spread_1m_pct is the unit-free
-- percentage of the top-of-book mid.  NULL = a side lacked the target depth.
-- Converted to RUB at query time via moex_fx_rates (forward-filled USDRUBF).

CREATE TABLE IF NOT EXISTS moex_orderbook_spread (
    bucket        TIMESTAMPTZ      NOT NULL,
    ticker        TEXT             NOT NULL,
    spread_1m_usd DOUBLE PRECISION,
    spread_1m_pct DOUBLE PRECISION,
    PRIMARY KEY (bucket, ticker)
);

CREATE INDEX IF NOT EXISTS idx_moex_ob_spread_ticker_bucket
    ON moex_orderbook_spread (ticker, bucket DESC);
