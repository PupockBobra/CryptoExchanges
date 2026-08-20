-- Switch BTC/ETH/SOL daily volume source from SPOT to PERPETUAL futures.
-- Applied automatically by the timescaledb Docker entrypoint on first run.
--
-- The OHLCV backfill now fetches perpetual-futures volume for these three
-- canonicals (see CRYPTO_PERP_OVERRIDES in backend/app/exchanges.py), storing
-- it under the same canonical symbol. Any pre-existing rows were spot-sourced,
-- so we delete them and let the backfill repopulate the full history from perps.
--
-- On a fresh database this is a harmless no-op (the table is empty). On an
-- existing database, run this once so old spot bars don't linger alongside the
-- new perp bars:
--   docker compose exec timescaledb psql -U postgres -d crypto \
--     -f /docker-entrypoint-initdb.d/004_reset_crypto_majors_perp_volume.sql

DELETE FROM ohlcv_daily
WHERE symbol IN ('BTC/USDT', 'ETH/USDT', 'SOL/USDT');
