-- Percentage "spread on volume" for SPB perps, alongside the existing absolute
-- (per-contract USD) spread.  spread_*_pct is (P_aver_ask - P_aver_bid) / P_mid
-- * 100, P_mid = (P_aver_ask + P_aver_bid) / 2 — unit-free (the ratio cancels lot
-- and the fx rate), so it needs no USD→RUB conversion.  Same 15-minute buckets,
-- same 1 млн / 10 млн ₽ depth targets.  NULL when the book lacked V/2 of depth.
-- History accrues from first run; it cannot be backfilled.

ALTER TABLE spb_orderbook_spread
    ADD COLUMN IF NOT EXISTS spread_1m_pct  DOUBLE PRECISION;
ALTER TABLE spb_orderbook_spread
    ADD COLUMN IF NOT EXISTS spread_10m_pct DOUBLE PRECISION;
