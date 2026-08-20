-- MM FORTS futures "spread on volume" (AVG_SPREAD) history.
--
-- One row per front-month underlying (ASSETCODE) × 15-min bucket, across the six
-- FORTS collections (index / shares / currency / interest / commodity / ofz).
-- The universe is resolved daily from ISS (front-month + STEPPRICE/MINSTEP), the
-- book is streamed via Finam under MIC RTSX, and the spread is measured for a
-- 1 000 000 ₽-per-side depth target (walking the book).
--
--   spread_abs = P_aver_ask − P_aver_bid, in the instrument's own quote unit
--                (₽ for shares & most FX, $ for USR contracts, points for index
--                futures) — stored and served as-is, NO currency conversion.
--   spread_pct = spread_abs / top-of-book mid × 100 — unit-free.
--   group_id   = the FORTS collection id, so a tab queries just its instruments.
-- NULL = a side lacked the target depth (illiquid).

CREATE TABLE IF NOT EXISTS mm_orderbook_spread (
    bucket     TIMESTAMPTZ      NOT NULL,
    ticker     TEXT             NOT NULL,
    group_id   TEXT             NOT NULL,
    spread_abs DOUBLE PRECISION,
    spread_pct DOUBLE PRECISION,
    PRIMARY KEY (bucket, ticker)
);

CREATE INDEX IF NOT EXISTS idx_mm_ob_spread_group_bucket
    ON mm_orderbook_spread (group_id, ticker, bucket DESC);
