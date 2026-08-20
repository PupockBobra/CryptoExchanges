-- Daily FORTS turnover per ISS ASSETCODE — numerator of the OKR ratio
-- (Cryptoexchanges → OKR, 20.08.2026).
--
-- Separate from `moex_daily_value`: that table holds the ~13 curated assets
-- keyed by the app's internal codes (BR / GD / SV …) and is joined to canonical
-- crypto symbols by CASE blocks all over timescale.py.  Here the key is the raw
-- ISS ASSETCODE and the table covers EVERY FORTS asset (~190 per day), so the
-- OKR baskets are a config filter that can change without a re-backfill.
--
-- Filled by a market-level day sweep (one ISS request per 100 contracts), not by
-- per-assetcode SECID discovery — see app/moex/fetcher.fetch_market_value_by_assetcode.
-- VALUE is already in roubles, so nothing is converted on the way in.

CREATE TABLE IF NOT EXISTS okr_moex_daily (
    date       DATE           NOT NULL,
    asset_code TEXT           NOT NULL,
    value_rub  NUMERIC(20, 2) NOT NULL,
    PRIMARY KEY (date, asset_code)
);

CREATE INDEX IF NOT EXISTS idx_okr_moex_daily_date
    ON okr_moex_daily (date DESC);
