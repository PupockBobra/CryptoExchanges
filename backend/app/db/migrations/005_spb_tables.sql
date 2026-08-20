-- SPB Exchange perpetual-futures daily turnover (via Finam TradeAPI).
-- Applied automatically by the timescaledb Docker entrypoint on first run;
-- init_db() also creates this table inline so existing DBs self-heal.
--
-- turnover_usd is stored in USD (Finam quotes SPB perps in USD); the API serves
-- exact turnover only for the current day, so historical rows hold an
-- approximation (volume × typical price). USD→RUB happens at query time via
-- moex_fx_rates, the same USDRUBF rate used by the crypto/MOEX volume pages.

CREATE TABLE IF NOT EXISTS spb_daily_volume (
    date         DATE           NOT NULL,
    ticker       TEXT           NOT NULL,
    volume       NUMERIC(20, 2) NOT NULL,
    turnover_usd NUMERIC(20, 2) NOT NULL,
    PRIMARY KEY (date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_spb_daily_volume_date
    ON spb_daily_volume (date DESC);
