-- Time-weighted "spread on volume" (AVG_SPREAD) history for SPB perps.
--
-- One row per 15-minute bucket per ticker.  spread_*_usd is the per-contract
-- USD spread — (P_aver_ask - P_aver_bid) * lot — to trade a given RUB volume
-- (half per side, walking the book).  NULL means the book lacked V/2 of depth
-- on at least one side (illiquid → no line on the chart).  Converted to RUB at
-- query time via moex_fx_rates (same forward-filled USDRUBF as everything else).

CREATE TABLE IF NOT EXISTS spb_orderbook_spread (
    bucket         TIMESTAMPTZ      NOT NULL,
    ticker         TEXT             NOT NULL,
    spread_1m_usd  DOUBLE PRECISION,
    spread_10m_usd DOUBLE PRECISION,
    PRIMARY KEY (bucket, ticker)
);

CREATE INDEX IF NOT EXISTS idx_spb_ob_spread_ticker_bucket
    ON spb_orderbook_spread (ticker, bucket DESC);
