-- Order-book snapshot capture for the MM-presence estimator (SPB perps).
--
-- One row per price level per snapshot.  Unlike every other table in this
-- schema this is RAW market microstructure, not an aggregate: the whole point is
-- that the detector's thresholds (persistence, volume tolerance, symmetry, bin
-- width) are re-tunable after the fact, which is impossible once the levels have
-- been reduced to a number.
--
-- Sampling: one snapshot per instrument per 5 s, on the epoch-aligned grid, only
-- during SPB trading hours, top 20 levels per side.  Order-book depth CANNOT be
-- backfilled (Finam serves only the live book), so history accrues from the
-- first run onward — never retroactively.
--
-- Sizing (measured, not guessed): ~143 rows/second of capture ≈ 8.8 M rows and
-- ~1.2 GB per trading day, compressing 7.1×.  Hence 6-hour chunks (the weekly
-- default would keep ~8 days uncompressed, since a compression policy only fires
-- once a chunk's whole range is past the threshold), compression after 12 hours,
-- and 14-day retention — the analysis window the page offers is days, not months.

CREATE TABLE IF NOT EXISTS ob_snapshot_level (
    ts        TIMESTAMPTZ      NOT NULL,
    symbol    TEXT             NOT NULL,
    side      TEXT             NOT NULL,   -- 'bid' | 'ask'
    level_idx SMALLINT         NOT NULL,   -- 0 = best price on that side
    price     DOUBLE PRECISION NOT NULL,
    volume    DOUBLE PRECISION NOT NULL    -- contracts resting at that price
);

SELECT create_hypertable('ob_snapshot_level', 'ts', if_not_exists => TRUE,
                         chunk_time_interval => INTERVAL '6 hours');

ALTER TABLE ob_snapshot_level SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, side',
    timescaledb.compress_orderby   = 'ts DESC, level_idx'
);
SELECT add_compression_policy('ob_snapshot_level', INTERVAL '12 hours', if_not_exists => TRUE);
SELECT add_retention_policy('ob_snapshot_level', INTERVAL '14 days', if_not_exists => TRUE);

-- Also the dedup key: a restart mid-grid-point must not double-write a snapshot.
CREATE UNIQUE INDEX IF NOT EXISTS ob_snapshot_level_uniq
    ON ob_snapshot_level (ts, symbol, side, level_idx);

CREATE INDEX IF NOT EXISTS idx_ob_snapshot_symbol_ts
    ON ob_snapshot_level (symbol, ts DESC);

-- Capture bookkeeping.  The miss ratio is not decoration: "the level was there
-- in 92% of snapshots" means nothing if 30% of the snapshots were never taken,
-- so the page shows this next to every estimate.
CREATE TABLE IF NOT EXISTS ob_capture_session (
    id          SERIAL      PRIMARY KEY,
    symbol      TEXT        NOT NULL,
    started     TIMESTAMPTZ NOT NULL,
    ended       TIMESTAMPTZ,
    step_sec    INTEGER     NOT NULL,
    n_snapshots INTEGER     NOT NULL DEFAULT 0,
    n_missed    INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ob_capture_symbol
    ON ob_capture_session (symbol, started DESC);
