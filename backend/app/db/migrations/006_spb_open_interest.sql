-- SPB Exchange perpetual-futures daily open interest (via the exchange's own
-- public API: /api/im/v1/tradingResults/futuresDay/all — no auth, no Finam token).
--
-- Per instrument the end-of-day (session=3) snapshot is stored:
--   oi_contracts  — open positions in contracts (totalOpenPosition)
--   oi_usd        — open-position notional in USD (totalOpenPositionVolume)
-- USD→RUB happens at query time via moex_fx_rates (USDRUBF, forward-filled),
-- the same conversion used by the turnover and crypto OI pages so charts compare.

CREATE TABLE IF NOT EXISTS spb_open_interest (
    date         DATE           NOT NULL,
    ticker       TEXT           NOT NULL,
    oi_contracts NUMERIC(20, 2) NOT NULL,
    oi_usd       NUMERIC(20, 2) NOT NULL,
    PRIMARY KEY (date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_spb_open_interest_date
    ON spb_open_interest (date DESC);
