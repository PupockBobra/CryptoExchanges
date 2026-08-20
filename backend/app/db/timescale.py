import asyncio
import json
import logging
import re
import asyncpg
from datetime import date, timedelta
from app.config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    settings.database_url, min_size=2, max_size=10,
                )
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

            CREATE TABLE IF NOT EXISTS price_ticks (
                ts          TIMESTAMPTZ NOT NULL,
                exchange    TEXT        NOT NULL,
                symbol      TEXT        NOT NULL,
                bid         DOUBLE PRECISION,
                ask         DOUBLE PRECISION,
                last        DOUBLE PRECISION
            );

            SELECT create_hypertable('price_ticks', 'ts', if_not_exists => TRUE);

            CREATE INDEX IF NOT EXISTS idx_price_ticks_sym_ex
                ON price_ticks (symbol, exchange, ts DESC);

            CREATE TABLE IF NOT EXISTS arbitrage_alerts (
                ts              TIMESTAMPTZ NOT NULL,
                symbol          TEXT        NOT NULL,
                buy_exchange    TEXT        NOT NULL,
                sell_exchange   TEXT        NOT NULL,
                buy_price       DOUBLE PRECISION,
                sell_price      DOUBLE PRECISION,
                spread_pct      DOUBLE PRECISION
            );

            SELECT create_hypertable('arbitrage_alerts', 'ts', if_not_exists => TRUE);

            CREATE TABLE IF NOT EXISTS instruments (
                id          SERIAL      PRIMARY KEY,
                canonical   TEXT        NOT NULL UNIQUE,
                type        TEXT        NOT NULL DEFAULT 'spot',
                base_asset  TEXT        NOT NULL DEFAULT '',
                quote_asset TEXT        NOT NULL DEFAULT '',
                description TEXT        NOT NULL DEFAULT '',
                enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
                aliases     JSONB       NOT NULL DEFAULT '{}',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ohlcv_daily (
                ts           TIMESTAMPTZ      NOT NULL,
                symbol       TEXT             NOT NULL,
                exchange     TEXT             NOT NULL,
                open         DOUBLE PRECISION NOT NULL DEFAULT 0,
                high         DOUBLE PRECISION NOT NULL DEFAULT 0,
                low          DOUBLE PRECISION NOT NULL DEFAULT 0,
                close        DOUBLE PRECISION NOT NULL DEFAULT 0,
                base_volume  DOUBLE PRECISION NOT NULL DEFAULT 0,
                quote_volume DOUBLE PRECISION NOT NULL DEFAULT 0
            );
        """)
        # create_hypertable must run outside multi-statement string
        async with pool.acquire() as conn2:
            await conn2.execute("""
                SELECT create_hypertable(
                    'ohlcv_daily', 'ts',
                    if_not_exists => TRUE,
                    migrate_data  => TRUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ohlcv_daily_uniq
                    ON ohlcv_daily (ts, symbol, exchange);
            """)

        # ── Hourly OHLCV (Hourly Volume page) — migration 015 ────────────────────
        # Separate from ohlcv_daily on purpose: every existing volume query reads
        # ohlcv_daily unqualified, so mixing granularities in one table would
        # double-count everywhere.  Retention is applied below with the others.
        async with pool.acquire() as conn2b:
            await conn2b.execute("""
                CREATE TABLE IF NOT EXISTS ohlcv_hourly (
                    ts           TIMESTAMPTZ      NOT NULL,
                    symbol       TEXT             NOT NULL,
                    exchange     TEXT             NOT NULL,
                    open         DOUBLE PRECISION NOT NULL DEFAULT 0,
                    high         DOUBLE PRECISION NOT NULL DEFAULT 0,
                    low          DOUBLE PRECISION NOT NULL DEFAULT 0,
                    close        DOUBLE PRECISION NOT NULL DEFAULT 0,
                    base_volume  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    quote_volume DOUBLE PRECISION NOT NULL DEFAULT 0
                );
            """)
            await conn2b.execute("""
                SELECT create_hypertable(
                    'ohlcv_hourly', 'ts',
                    if_not_exists => TRUE,
                    migrate_data  => TRUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ohlcv_hourly_uniq
                    ON ohlcv_hourly (ts, symbol, exchange);
            """)

        # ── 1-minute continuous aggregate (used by the realtime chart API) ────────
        # Continuous aggregates and policies are idempotent at the SQL level via
        # IF NOT EXISTS / if_not_exists, but some TimescaleDB minor versions
        # raise on duplicate views.  We log instead of swallowing so genuine
        # failures (permissions, missing extension) are visible.
        async with pool.acquire() as conn3:
            try:
                await conn3.execute("""
                    CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_1m
                    WITH (timescaledb.continuous) AS
                    SELECT time_bucket('1 minute', ts) AS bucket,
                           exchange,
                           symbol,
                           first(last, ts)  AS open,
                           max(last)        AS high,
                           min(last)        AS low,
                           last(last, ts)   AS close,
                           count(*)         AS ticks
                    FROM price_ticks
                    GROUP BY bucket, exchange, symbol
                    WITH NO DATA
                """)
            except Exception as exc:
                log.warning("init_db: create ohlcv_1m view: %s", exc)

            try:
                await conn3.execute("""
                    SELECT add_continuous_aggregate_policy('ohlcv_1m',
                        start_offset      => INTERVAL '7 days',
                        end_offset        => INTERVAL '1 minute',
                        schedule_interval => INTERVAL '1 minute',
                        if_not_exists     => TRUE
                    )
                """)
            except Exception as exc:
                log.warning("init_db: add ohlcv_1m policy: %s", exc)

        # ── Funding rates (settled, for backtesting) ─────────────────────────────
        async with pool.acquire() as conn_fr:
            try:
                await conn_fr.execute("""
                    CREATE TABLE IF NOT EXISTS funding_rates (
                        time           TIMESTAMPTZ      NOT NULL,
                        symbol         TEXT             NOT NULL,
                        exchange       TEXT             NOT NULL,
                        rate           DOUBLE PRECISION NOT NULL,
                        interval_hours SMALLINT         NOT NULL DEFAULT 8,
                        UNIQUE (time, symbol, exchange)
                    );
                """)
            except Exception as exc:
                log.warning("init_db: create funding_rates table: %s", exc)
            try:
                await conn_fr.execute("""
                    SELECT create_hypertable(
                        'funding_rates', 'time',
                        if_not_exists => TRUE,
                        migrate_data  => TRUE
                    );
                """)
            except Exception as exc:
                log.warning("init_db: hypertable funding_rates: %s", exc)
            try:
                await conn_fr.execute("""
                    CREATE INDEX IF NOT EXISTS idx_funding_rates_sym_ex
                        ON funding_rates (symbol, exchange, time DESC);
                """)
            except Exception as exc:
                log.warning("init_db: index funding_rates: %s", exc)

        # ── MOEX tables ──────────────────────────────────────────────────────────
        async with pool.acquire() as conn_moex:
            await conn_moex.execute("""
                CREATE TABLE IF NOT EXISTS moex_daily_value (
                    date       DATE           NOT NULL,
                    asset_code TEXT           NOT NULL,
                    value_rub  NUMERIC(20, 2) NOT NULL,
                    PRIMARY KEY (date, asset_code)
                );
                CREATE INDEX IF NOT EXISTS idx_moex_daily_value_date
                    ON moex_daily_value (date DESC);

                CREATE TABLE IF NOT EXISTS moex_fx_rates (
                    date   DATE           PRIMARY KEY,
                    usdrub NUMERIC(12, 4) NOT NULL
                );

                -- Numerator of the OKR ratio: daily FORTS turnover of EVERY
                -- asset, keyed by the raw ISS ASSETCODE (see migration 019).
                CREATE TABLE IF NOT EXISTS okr_moex_daily (
                    date       DATE           NOT NULL,
                    asset_code TEXT           NOT NULL,
                    value_rub  NUMERIC(20, 2) NOT NULL,
                    PRIMARY KEY (date, asset_code)
                );
                CREATE INDEX IF NOT EXISTS idx_okr_moex_daily_date
                    ON okr_moex_daily (date DESC);
            """)

        # ── SPB Exchange perp turnover ───────────────────────────────────────────
        async with pool.acquire() as conn_spb:
            await conn_spb.execute("""
                CREATE TABLE IF NOT EXISTS spb_daily_volume (
                    date         DATE           NOT NULL,
                    ticker       TEXT           NOT NULL,
                    volume       NUMERIC(20, 2) NOT NULL,
                    turnover_usd NUMERIC(20, 2) NOT NULL,
                    PRIMARY KEY (date, ticker)
                );
                CREATE INDEX IF NOT EXISTS idx_spb_daily_volume_date
                    ON spb_daily_volume (date DESC);

                CREATE TABLE IF NOT EXISTS spb_open_interest (
                    date         DATE           NOT NULL,
                    ticker       TEXT           NOT NULL,
                    oi_contracts NUMERIC(20, 2) NOT NULL,
                    oi_usd       NUMERIC(20, 2) NOT NULL,
                    PRIMARY KEY (date, ticker)
                );
                CREATE INDEX IF NOT EXISTS idx_spb_open_interest_date
                    ON spb_open_interest (date DESC);

                -- Daily funding figures per perp, uploaded from the СПБ Биржа
                -- Telegram CSVs (one row per instrument × trading day).  Values
                -- are stored verbatim from the file: pct_year/pct_day are the
                -- annualised / daily funding rate (%), fund_curr the per-contract
                -- funding in USD, mean_price/mean_index the day's average perp
                -- and index prices.  No fx conversion — this is a rate, not a
                -- turnover.
                CREATE TABLE IF NOT EXISTS spb_funding (
                    date       DATE             NOT NULL,
                    ticker     TEXT             NOT NULL,
                    pct_year   DOUBLE PRECISION,
                    pct_day    DOUBLE PRECISION,
                    fund_curr  DOUBLE PRECISION,
                    mean_price DOUBLE PRECISION,
                    mean_index DOUBLE PRECISION,
                    PRIMARY KEY (date, ticker)
                );
                CREATE INDEX IF NOT EXISTS idx_spb_funding_date
                    ON spb_funding (date DESC);

                CREATE TABLE IF NOT EXISTS spb_orderbook_spread (
                    bucket         TIMESTAMPTZ      NOT NULL,
                    ticker         TEXT             NOT NULL,
                    spread_1m_usd  DOUBLE PRECISION,
                    spread_10m_usd DOUBLE PRECISION,
                    spread_1m_pct  DOUBLE PRECISION,
                    spread_10m_pct DOUBLE PRECISION,
                    PRIMARY KEY (bucket, ticker)
                );
                ALTER TABLE spb_orderbook_spread
                    ADD COLUMN IF NOT EXISTS spread_1m_pct  DOUBLE PRECISION;
                ALTER TABLE spb_orderbook_spread
                    ADD COLUMN IF NOT EXISTS spread_10m_pct DOUBLE PRECISION;
                CREATE INDEX IF NOT EXISTS idx_spb_ob_spread_ticker_bucket
                    ON spb_orderbook_spread (ticker, bucket DESC);

                -- MOEX crypto-index futures spread (overlaid on the 5 crypto SPB
                -- Order Book cards).  ticker = the SPB crypto ticker it maps to.
                CREATE TABLE IF NOT EXISTS moex_orderbook_spread (
                    bucket        TIMESTAMPTZ      NOT NULL,
                    ticker        TEXT             NOT NULL,
                    spread_1m_usd DOUBLE PRECISION,
                    spread_1m_pct DOUBLE PRECISION,
                    PRIMARY KEY (bucket, ticker)
                );
                CREATE INDEX IF NOT EXISTS idx_moex_ob_spread_ticker_bucket
                    ON moex_orderbook_spread (ticker, bucket DESC);

                -- MM FORTS futures spread-on-volume (one row per front-month
                -- underlying × 15-min bucket).  spread_abs is in the instrument's
                -- own quote unit (₽ / $ / points); spread_pct is unit-free.
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
            """)

        # ── Futures Launches: derived listing dates ──────────────────────────────
        # Bitget / Hyperliquid publish no listing field, so the Launches page
        # derives it from the first traded daily candle — dozens of paginated
        # kline calls.  The answer never changes, so it is stored here instead of
        # being recomputed on every backend restart.
        async with pool.acquire() as conn_lf:
            await conn_lf.execute("""
                CREATE TABLE IF NOT EXISTS launch_first_trade (
                    exchange     TEXT NOT NULL,
                    symbol       TEXT NOT NULL,
                    first_traded DATE NOT NULL,
                    PRIMARY KEY (exchange, symbol)
                );
            """)

        # ── Crypto-exchange equity (stock) perp turnover ──────────────────────────
        async with pool.acquire() as conn_stk:
            await conn_stk.execute("""
                CREATE TABLE IF NOT EXISTS stock_daily_volume (
                    date      DATE           NOT NULL,
                    exchange  TEXT           NOT NULL,
                    ticker    TEXT           NOT NULL,
                    quote_usd NUMERIC(24, 2) NOT NULL,
                    PRIMARY KEY (date, exchange, ticker)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_daily_volume_date
                    ON stock_daily_volume (date DESC);
            """)

        # Hourly counterpart, feeding the hour-of-day profile on TradFi Market
        # Share.  Separate from ohlcv_hourly on purpose — see migration 016.
        # Retention is applied below with the others.
        async with pool.acquire() as conn_stk_h:
            await conn_stk_h.execute("""
                CREATE TABLE IF NOT EXISTS stock_hourly_volume (
                    hour      TIMESTAMPTZ    NOT NULL,
                    exchange  TEXT           NOT NULL,
                    ticker    TEXT           NOT NULL,
                    quote_usd NUMERIC(24, 2) NOT NULL
                );
            """)
            await conn_stk_h.execute("""
                SELECT create_hypertable(
                    'stock_hourly_volume', 'hour',
                    if_not_exists => TRUE,
                    migrate_data  => TRUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS stock_hourly_volume_uniq
                    ON stock_hourly_volume (hour, exchange, ticker);
            """)

        # ── Top-N crypto perps per exchange (asset-group charts) ──────────────────
        # See migration 017.  Retention for the hourly table is applied below.
        async with pool.acquire() as conn_ctop:
            await conn_ctop.execute("""
                CREATE TABLE IF NOT EXISTS crypto_top_daily_volume (
                    date      DATE           NOT NULL,
                    exchange  TEXT           NOT NULL,
                    symbol    TEXT           NOT NULL,
                    quote_usd NUMERIC(24, 2) NOT NULL,
                    PRIMARY KEY (date, exchange, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_crypto_top_daily_date
                    ON crypto_top_daily_volume (date DESC);
                CREATE TABLE IF NOT EXISTS crypto_top_hourly_volume (
                    hour      TIMESTAMPTZ    NOT NULL,
                    exchange  TEXT           NOT NULL,
                    symbol    TEXT           NOT NULL,
                    quote_usd NUMERIC(24, 2) NOT NULL
                );
            """)
            await conn_ctop.execute("""
                SELECT create_hypertable(
                    'crypto_top_hourly_volume', 'hour',
                    if_not_exists => TRUE,
                    migrate_data  => TRUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS crypto_top_hourly_uniq
                    ON crypto_top_hourly_volume (hour, exchange, symbol);
            """)

        # ── Raw order-book snapshots (MM-presence estimator) ─────────────────────
        # See migration 018.  Raw levels on purpose: the detector's thresholds are
        # re-tunable after the fact, which an aggregate would foreclose.  Big and
        # write-heavy, so: hypertable + compression after a day + 14-day retention
        # (applied below with the others).
        async with pool.acquire() as conn_ob:
            await conn_ob.execute("""
                CREATE TABLE IF NOT EXISTS ob_snapshot_level (
                    ts        TIMESTAMPTZ      NOT NULL,
                    symbol    TEXT             NOT NULL,
                    side      TEXT             NOT NULL,
                    level_idx SMALLINT         NOT NULL,
                    price     DOUBLE PRECISION NOT NULL,
                    volume    DOUBLE PRECISION NOT NULL
                );
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
            """)
            await conn_ob.execute("""
                SELECT create_hypertable(
                    'ob_snapshot_level', 'ts',
                    if_not_exists      => TRUE,
                    migrate_data       => TRUE,
                    chunk_time_interval => INTERVAL '6 hours'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ob_snapshot_level_uniq
                    ON ob_snapshot_level (ts, symbol, side, level_idx);
                CREATE INDEX IF NOT EXISTS idx_ob_snapshot_symbol_ts
                    ON ob_snapshot_level (symbol, ts DESC);
            """)
            # Six-hour chunks, not the 7-day default, and that is a disk decision
            # rather than a query one: the compression policy only fires on chunks
            # whose whole range is older than its threshold, so weekly chunks would
            # hold ~8 days of UNCOMPRESSED capture (~1.2 GB per trading day at
            # 15 instruments × 5 s × 20 levels) before anything shrank.
            await conn_ob.execute(
                "SELECT set_chunk_time_interval('ob_snapshot_level', INTERVAL '6 hours')"
            )
            # Compression pays here (unlike the hourly ETL tables) because rows
            # are written once and never rewritten — nothing re-visits a past
            # snapshot, so a compressed chunk is never decompressed to be updated.
            # Measured on real capture: 7.1× (3888 kB → 544 kB).
            try:
                await conn_ob.execute("""
                    ALTER TABLE ob_snapshot_level SET (
                        timescaledb.compress,
                        timescaledb.compress_segmentby = 'symbol, side',
                        timescaledb.compress_orderby   = 'ts DESC, level_idx'
                    );
                """)
                await conn_ob.execute(
                    "SELECT add_compression_policy('ob_snapshot_level', INTERVAL '12 hours', if_not_exists => TRUE)"
                )
            except Exception as exc:
                log.warning("init_db: compression for ob_snapshot_level: %s", exc)

        # ── Open Interest ────────────────────────────────────────────────────────
        async with pool.acquire() as conn_oi:
            try:
                await conn_oi.execute("""
                    CREATE TABLE IF NOT EXISTS open_interest (
                        ts           TIMESTAMPTZ      NOT NULL,
                        exchange     TEXT             NOT NULL,
                        symbol       TEXT             NOT NULL,
                        oi_contracts DOUBLE PRECISION,
                        oi_usdt      DOUBLE PRECISION,
                        UNIQUE (ts, exchange, symbol)
                    );
                """)
            except Exception as exc:
                log.warning("init_db: create open_interest table: %s", exc)
            try:
                await conn_oi.execute("""
                    SELECT create_hypertable(
                        'open_interest', 'ts',
                        if_not_exists => TRUE,
                        migrate_data  => TRUE
                    );
                """)
            except Exception as exc:
                log.warning("init_db: hypertable open_interest: %s", exc)
            try:
                await conn_oi.execute("""
                    CREATE INDEX IF NOT EXISTS idx_oi_sym_ex
                        ON open_interest (symbol, exchange, ts DESC);
                """)
            except Exception as exc:
                log.warning("init_db: index open_interest: %s", exc)

        # ── Retention policies (idempotent: drop old, add at desired duration) ────
        # Note: positional args required — named `if_not_exists =>` not supported
        # in all TimescaleDB versions.
        async with pool.acquire() as conn4:
            for table, interval in [
                ('price_ticks',      '7 days'),
                ('ohlcv_1m',         '7 days'),
                ('arbitrage_alerts', '30 days'),
                ('ohlcv_hourly',     '90 days'),
                ('stock_hourly_volume', '45 days'),
                ('crypto_top_hourly_volume', '45 days'),
                ('ob_snapshot_level', '14 days'),
                ('funding_rates',    '2 years'),
            ]:
                try:
                    # remove_retention_policy(relation, if_not_exists=true)
                    await conn4.execute(
                        f"SELECT remove_retention_policy('{table}', true)"
                    )
                    await conn4.execute(
                        f"SELECT add_retention_policy('{table}', INTERVAL '{interval}')"
                    )
                except Exception as exc:
                    # table may not exist yet (ohlcv_1m on first boot)
                    log.warning("init_db: retention for %s: %s", table, exc)


# ── Price ticks ──────────────────────────────────────────────────────────────

async def insert_tick(exchange: str, symbol: str, bid: float, ask: float, last: float):
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO price_ticks (ts, exchange, symbol, bid, ask, last) VALUES (NOW(), $1, $2, $3, $4, $5)",
        exchange, symbol, bid, ask, last,
    )


async def insert_alert(symbol: str, buy_ex: str, sell_ex: str, buy_p: float, sell_p: float, spread: float):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO arbitrage_alerts
           (ts, symbol, buy_exchange, sell_exchange, buy_price, sell_price, spread_pct)
           VALUES (NOW(), $1, $2, $3, $4, $5, $6)""",
        symbol, buy_ex, sell_ex, buy_p, sell_p, spread,
    )


def _parse_interval(s: str) -> timedelta:
    m = re.match(r'^(\d+)\s*(second|minute|hour|day)s?$', s.strip().lower())
    if not m:
        raise ValueError(f"invalid interval: {s!r} (expected e.g. '1 minute', '5 minutes', '1 hour')")
    n, unit = int(m.group(1)), m.group(2)
    return {
        'second': timedelta(seconds=n),
        'minute': timedelta(minutes=n),
        'hour':   timedelta(hours=n),
        'day':    timedelta(days=n),
    }[unit]


async def fetch_ohlcv(symbol: str, exchange: str, interval: str = "1 minute", limit: int = 200):
    """
    Fetch OHLCV bars from the pre-aggregated ohlcv_1m continuous aggregate.
    Falls back to querying price_ticks directly if the view has no data yet
    (e.g. first few minutes after a fresh deployment).
    """
    pool = await get_pool()
    td = _parse_interval(interval)
    lookback = td * limit
    rows = await pool.fetch(
        """
        SELECT time_bucket($1, bucket) AS bucket,
               exchange, symbol,
               first(open, bucket) AS open,
               max(high)           AS high,
               min(low)            AS low,
               last(close, bucket) AS close,
               sum(ticks)::int     AS ticks
        FROM ohlcv_1m
        WHERE symbol = $2 AND exchange = $3
          AND bucket > NOW() - $4::interval
        GROUP BY time_bucket($1, bucket), exchange, symbol
        ORDER BY 1 DESC
        """,
        td, symbol, exchange, lookback,
    )
    if rows:
        return rows
    # Fallback: aggregate directly from price_ticks (first minutes after cold start).
    return await pool.fetch(
        """
        SELECT time_bucket($1, ts) AS bucket,
               exchange, symbol,
               first(last, ts) AS open,
               max(last)       AS high,
               min(last)       AS low,
               last(last, ts)  AS close,
               count(*)::int   AS ticks
        FROM price_ticks
        WHERE symbol = $2 AND exchange = $3
          AND ts > NOW() - $4::interval
        GROUP BY bucket, exchange, symbol
        ORDER BY bucket DESC
        """,
        td, symbol, exchange, lookback,
    )


async def fetch_latest_ticks(symbol: str):
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT DISTINCT ON (exchange) exchange, symbol, bid, ask, last, ts
        FROM price_ticks
        WHERE symbol = $1
        ORDER BY exchange, ts DESC
        """,
        symbol,
    )


async def fetch_recent_alerts(symbol: str | None = None, limit: int = 50):
    pool = await get_pool()
    if symbol:
        return await pool.fetch(
            "SELECT * FROM arbitrage_alerts WHERE symbol = $1 ORDER BY ts DESC LIMIT $2",
            symbol, limit,
        )
    return await pool.fetch(
        "SELECT * FROM arbitrage_alerts ORDER BY ts DESC LIMIT $1", limit
    )


# ── Instruments ──────────────────────────────────────────────────────────────

async def fetch_instruments(enabled_only: bool = False):
    pool = await get_pool()
    q = "SELECT * FROM instruments"
    if enabled_only:
        q += " WHERE enabled = TRUE"
    q += " ORDER BY type, canonical"
    return await pool.fetch(q)


async def create_instrument(
    canonical: str,
    type_: str,
    base_asset: str,
    quote_asset: str,
    description: str,
    enabled: bool,
    aliases: dict,
) -> asyncpg.Record:
    pool = await get_pool()
    return await pool.fetchrow(
        """
        INSERT INTO instruments (canonical, type, base_asset, quote_asset, description, enabled, aliases)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *
        """,
        canonical, type_, base_asset, quote_asset, description, enabled,
        json.dumps(aliases),
    )


# Whitelist of columns that may be updated.  Hard-coded to prevent
# SQL injection: column names cannot be parameterised, so they must
# never come from user input.
_INSTRUMENT_UPDATABLE_COLUMNS = frozenset({
    "canonical", "type", "base_asset", "quote_asset",
    "description", "enabled", "aliases",
})


async def update_instrument(id_: int, **fields) -> asyncpg.Record | None:
    """Update whitelisted fields on an instrument row."""
    pool = await get_pool()

    # Drop any field not in the whitelist
    safe = {k: v for k, v in fields.items() if k in _INSTRUMENT_UPDATABLE_COLUMNS}
    if not safe:
        return await pool.fetchrow("SELECT * FROM instruments WHERE id = $1", id_)

    # Serialize aliases dict to JSON string if present
    if "aliases" in safe and isinstance(safe["aliases"], dict):
        safe["aliases"] = json.dumps(safe["aliases"])

    cols = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(safe))
    vals = list(safe.values())
    return await pool.fetchrow(
        f"UPDATE instruments SET {cols}, updated_at = NOW() WHERE id = $1 RETURNING *",
        id_, *vals,
    )


async def delete_instrument(id_: int) -> bool:
    pool = await get_pool()
    result = await pool.execute("DELETE FROM instruments WHERE id = $1", id_)
    return result == "DELETE 1"


# ── Daily OHLCV ──────────────────────────────────────────────────────────────

async def upsert_ohlcv_daily(rows: list[tuple]) -> int:
    """
    Bulk upsert daily OHLCV rows.
    Each tuple: (ts, symbol, exchange, open, high, low, close, base_volume, quote_volume)
    Returns the number of rows upserted.
    """
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO ohlcv_daily
                    (ts, symbol, exchange, open, high, low, close, base_volume, quote_volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (ts, symbol, exchange) DO UPDATE SET
                    open         = EXCLUDED.open,
                    high         = EXCLUDED.high,
                    low          = EXCLUDED.low,
                    close        = EXCLUDED.close,
                    base_volume  = EXCLUDED.base_volume,
                    quote_volume = EXCLUDED.quote_volume
                """,
                rows,
            )
    return len(rows)


async def fetch_ohlcv_daily(
    symbol: str,
    exchange: str | None = None,
    limit: int = 365,
) -> list[asyncpg.Record]:
    pool = await get_pool()
    # Bounding ts excludes empty future chunks so the planner doesn't lock
    # every chunk in the hypertable (otherwise blows past max_locks_per_transaction).
    #
    # LIMIT must apply to the MOST RECENT N rows, not the oldest. We take
    # them with ORDER BY ts DESC inside a subquery and re-sort ASC for the
    # frontend (which iterates chronologically).
    if exchange:
        return await pool.fetch(
            """
            SELECT * FROM (
                SELECT ts, symbol, exchange, open, high, low, close, base_volume, quote_volume
                FROM ohlcv_daily
                WHERE symbol = $1 AND exchange = $2
                  AND ts >= '2026-01-01'
                  AND ts <  CURRENT_DATE + INTERVAL '1 day'
                ORDER BY ts DESC
                LIMIT $3
            ) recent
            ORDER BY ts ASC
            """,
            symbol, exchange, limit,
        )
    # Aggregate across exchanges: sum volumes, OHLC from first/max/min/last exchange data
    return await pool.fetch(
        """
        SELECT * FROM (
            SELECT
                ts,
                $1::text                  AS symbol,
                'aggregate'::text         AS exchange,
                AVG(open)                 AS open,
                MAX(high)                 AS high,
                MIN(low)                  AS low,
                AVG(close)                AS close,
                SUM(base_volume)          AS base_volume,
                SUM(quote_volume)         AS quote_volume
            FROM ohlcv_daily
            WHERE symbol = $1
              AND ts >= '2026-01-01'
              AND ts <  CURRENT_DATE + INTERVAL '1 day'
            GROUP BY ts
            ORDER BY ts DESC
            LIMIT $2
        ) recent
        ORDER BY ts ASC
        """,
        symbol, limit,
    )


async def get_ohlcv_daily_latest_ts(symbol: str, exchange: str):
    """Return the most recent stored timestamp for this symbol × exchange pair."""
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT MAX(ts) FROM ohlcv_daily WHERE symbol = $1 AND exchange = $2",
        symbol, exchange,
    )


# ── Hourly OHLCV (Hourly Volume page) ─────────────────────────────────────────

async def upsert_ohlcv_hourly(rows: list[tuple]) -> int:
    """
    Bulk upsert hourly OHLCV rows.
    Each tuple: (ts, symbol, exchange, open, high, low, close, base_volume, quote_volume)
    """
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO ohlcv_hourly
                    (ts, symbol, exchange, open, high, low, close, base_volume, quote_volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (ts, symbol, exchange) DO UPDATE SET
                    open         = EXCLUDED.open,
                    high         = EXCLUDED.high,
                    low          = EXCLUDED.low,
                    close        = EXCLUDED.close,
                    base_volume  = EXCLUDED.base_volume,
                    quote_volume = EXCLUDED.quote_volume
                """,
                rows,
            )
    return len(rows)


async def get_ohlcv_hourly_latest_ts(symbol: str, exchange: str):
    """Most recent stored hourly bar for this symbol × exchange pair."""
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT MAX(ts) FROM ohlcv_hourly WHERE symbol = $1 AND exchange = $2",
        symbol, exchange,
    )


async def fetch_hourly_volume_rub(days: int = 7) -> list[asyncpg.Record]:
    """
    Hourly volume in RUB per symbol × exchange over the last `days` days.

    Timestamps come back in UTC — the frontend shifts them to MSK with the same
    `toMsk` helper the spread charts use.  RUB conversion is the usual
    forward-filled USDRUBF join, so weekend/holiday hours keep their volume.

    The upper time bound is required: without it the planner locks every chunk
    in the hypertable (see the chunk-locking note in CLAUDE.md).
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            o.ts,
            o.symbol,
            o.exchange,
            ROUND((o.quote_volume * fx.usdrub)::numeric, 2) AS volume_rub
        FROM ohlcv_hourly o
        LEFT JOIN LATERAL (
            SELECT usdrub
            FROM moex_fx_rates
            WHERE date <= o.ts::date
            ORDER BY date DESC
            LIMIT 1
        ) fx ON TRUE
        WHERE o.ts >= date_trunc('hour', NOW()) - ($1::int * INTERVAL '1 day')
          AND o.ts <  date_trunc('hour', NOW()) + INTERVAL '1 hour'
          AND fx.usdrub IS NOT NULL
        ORDER BY o.ts, o.symbol, o.exchange
        """,
        days,
    )


async def fetch_hourly_profile_rub(days: int = 30) -> list[asyncpg.Record]:
    """
    Intraday profile: mean volume per MSK hour-of-day × symbol × exchange.

    The hour bucket is derived server-side (`AT TIME ZONE 'Europe/Moscow'`)
    because the hour label IS the grouping key here — unlike the time series,
    the frontend cannot shift it after aggregation.

    The mean is taken over the days that actually have a bar for that hour, so a
    pair listed midway through the window is not diluted by leading zeros.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            EXTRACT(hour FROM o.ts AT TIME ZONE 'Europe/Moscow')::int AS hour_msk,
            o.symbol,
            o.exchange,
            ROUND(AVG(o.quote_volume * fx.usdrub)::numeric, 2) AS avg_volume_rub,
            COUNT(*)                                           AS samples
        FROM ohlcv_hourly o
        LEFT JOIN LATERAL (
            SELECT usdrub
            FROM moex_fx_rates
            WHERE date <= o.ts::date
            ORDER BY date DESC
            LIMIT 1
        ) fx ON TRUE
        WHERE o.ts >= date_trunc('hour', NOW()) - ($1::int * INTERVAL '1 day')
          AND o.ts <  date_trunc('hour', NOW()) + INTERVAL '1 hour'
          AND fx.usdrub IS NOT NULL
        GROUP BY hour_msk, o.symbol, o.exchange
        ORDER BY hour_msk, o.symbol, o.exchange
        """,
        days,
    )


async def fetch_history_metrics() -> list[asyncpg.Record]:
    """
    Return ADTV YTD, ADTV this week, ADTV last week, and days of data
    for every symbol in ohlcv_daily.  Volumes are summed across exchanges
    per day before averaging, so the result represents true total market activity.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            symbol,
            ROUND(AVG(daily_total)::numeric, 2)                                     AS adtv_ytd,
            COUNT(*)                                                                  AS ytd_days,
            ROUND(AVG(daily_total) FILTER (
                WHERE ts >= date_trunc('week', NOW() AT TIME ZONE 'UTC')
            )::numeric, 2)                                                            AS adtv_week,
            COUNT(*) FILTER (
                WHERE ts >= date_trunc('week', NOW() AT TIME ZONE 'UTC')
            )                                                                         AS week_days,
            ROUND(AVG(daily_total) FILTER (
                WHERE ts >= date_trunc('week', (NOW() - INTERVAL '7 days') AT TIME ZONE 'UTC')
                  AND ts <  date_trunc('week', NOW() AT TIME ZONE 'UTC')
            )::numeric, 2)                                                            AS adtv_last_week,
            MAX(ts)                                                                   AS last_updated
        FROM (
            SELECT ts, symbol, SUM(quote_volume) AS daily_total
            FROM ohlcv_daily
            WHERE ts >= date_trunc('year', CURRENT_DATE)::date
              AND ts <  CURRENT_DATE + INTERVAL '1 day'
            GROUP BY ts, symbol
        ) daily
        GROUP BY symbol
        ORDER BY symbol
        """
    )


async def fetch_weekly_adtv_rub(exclude_bases: list[str] | None = None) -> list[asyncpg.Record]:
    """
    Weekly ADTV in RUB per symbol × exchange × ISO week.

    `exclude_bases` drops instruments from the ohlcv branch by base asset — used
    for the curated US stocks, whose turnover is served from the stock ETL.

    Crypto volumes are converted USDT → RUB using the daily USDRUBF rate from
    moex_fx_rates.  Missing dates (weekends/holidays/ISS gaps) are forward-filled
    from the most recent known rate via a LEFT JOIN LATERAL — INNER JOIN would
    silently drop crypto volume on every day MOEX wasn't trading.
    MOEX FORTS volumes are already in RUB and contribute a synthetic 'moex' exchange row.

    asset_code → canonical symbol mapping (FORTS):
      BR → BRN/USDT:USDT, NG → NATGAS/USDT:USDT, GD → XAU/USDT:USDT,
      SV → XAG/USDT:USDT, PT → XPT/USDT:USDT, PD → XPD/USDT:USDT
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH crypto_rub AS (
            SELECT
                date_trunc('week', o.ts)::date                          AS week_start,
                to_char(date_trunc('week', o.ts), 'Mon DD')             AS week_label,
                o.symbol,
                o.exchange,
                COUNT(*)                                                 AS days_in_week,
                ROUND(
                    (SUM(o.quote_volume * fx.usdrub) / NULLIF(COUNT(*), 0))::numeric, 2
                )                                                        AS adtv
            FROM ohlcv_daily o
            LEFT JOIN LATERAL (
                SELECT usdrub
                FROM moex_fx_rates
                WHERE date <= o.ts::date
                ORDER BY date DESC
                LIMIT 1
            ) fx ON TRUE
            WHERE o.ts >= date_trunc('year', CURRENT_DATE)::date
              AND o.ts <  CURRENT_DATE + INTERVAL '1 day'
              AND fx.usdrub IS NOT NULL
              AND split_part(o.symbol, '/', 1) <> ALL($1::text[])
            GROUP BY date_trunc('week', o.ts), o.symbol, o.exchange
        ),
        moex_rub AS (
            SELECT
                date_trunc('week', m.date)::date                        AS week_start,
                to_char(date_trunc('week', m.date), 'Mon DD')           AS week_label,
                CASE m.asset_code
                    WHEN 'BR' THEN 'BRN/USDT:USDT'
                    WHEN 'NG' THEN 'NATGAS/USDT:USDT'
                    WHEN 'GD' THEN 'XAU/USDT:USDT'
                    WHEN 'SV' THEN 'XAG/USDT:USDT'
                    WHEN 'PT' THEN 'XPT/USDT:USDT'
                    WHEN 'PD' THEN 'XPD/USDT:USDT'
                    WHEN 'NASD' THEN 'QQQ/USDT:USDT'
                    WHEN 'SPYF' THEN 'SPY/USDT:USDT'
                    WHEN 'BTC'  THEN 'BTC/USDT'
                    WHEN 'ETH'  THEN 'ETH/USDT'
                    WHEN 'SOL'  THEN 'SOL/USDT'
                    WHEN 'XRP'  THEN 'XRP/USDT'
                    WHEN 'TRX'  THEN 'TRX/USDT'
                END                                                      AS symbol,
                'moex'::text                                             AS exchange,
                COUNT(*)                                                 AS days_in_week,
                ROUND(
                    (SUM(m.value_rub) / NULLIF(COUNT(*), 0))::numeric, 2
                )                                                        AS adtv
            FROM moex_daily_value m
            WHERE m.date >= date_trunc('year', CURRENT_DATE)::date
              AND m.asset_code NOT IN ('XRP', 'TRX')
            GROUP BY date_trunc('week', m.date), m.asset_code
        )
        SELECT * FROM crypto_rub
        UNION ALL
        SELECT * FROM moex_rub
        ORDER BY week_start, symbol, exchange
        """,
        exclude_bases or [],
    )


async def fetch_daily_volume_rub(exclude_bases: list[str] | None = None) -> list[asyncpg.Record]:
    """
    Daily volume in RUB per symbol × exchange for the last 30 days.

    `exclude_bases` drops instruments from the ohlcv branch by base asset — used
    for the curated US stocks, whose turnover is served from the stock ETL.

    Crypto volumes: quote_volume_USDT × USDRUBF rate.  Missing FX days
    are forward-filled via LEFT JOIN LATERAL so weekend/holiday crypto
    volume is preserved instead of being silently dropped.
    MOEX FORTS: value_rub from moex_daily_value (already in RUB).
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH crypto_rub AS (
            SELECT
                o.ts::date                                                   AS date,
                to_char(o.ts::date, 'Mon DD')                                AS date_label,
                o.symbol,
                o.exchange,
                ROUND((o.quote_volume * fx.usdrub)::numeric, 2)              AS volume_rub
            FROM ohlcv_daily o
            LEFT JOIN LATERAL (
                SELECT usdrub
                FROM moex_fx_rates
                WHERE date <= o.ts::date
                ORDER BY date DESC
                LIMIT 1
            ) fx ON TRUE
            WHERE o.ts >= (CURRENT_DATE - INTERVAL '30 days')
              AND o.ts <  CURRENT_DATE + INTERVAL '1 day'
              AND fx.usdrub IS NOT NULL
              AND split_part(o.symbol, '/', 1) <> ALL($1::text[])
        ),
        moex_rub AS (
            SELECT
                m.date,
                to_char(m.date, 'Mon DD')                                    AS date_label,
                CASE m.asset_code
                    WHEN 'BR' THEN 'BRN/USDT:USDT'
                    WHEN 'NG' THEN 'NATGAS/USDT:USDT'
                    WHEN 'GD' THEN 'XAU/USDT:USDT'
                    WHEN 'SV' THEN 'XAG/USDT:USDT'
                    WHEN 'PT' THEN 'XPT/USDT:USDT'
                    WHEN 'PD' THEN 'XPD/USDT:USDT'
                    WHEN 'NASD' THEN 'QQQ/USDT:USDT'
                    WHEN 'SPYF' THEN 'SPY/USDT:USDT'
                    WHEN 'BTC'  THEN 'BTC/USDT'
                    WHEN 'ETH'  THEN 'ETH/USDT'
                    WHEN 'SOL'  THEN 'SOL/USDT'
                    WHEN 'XRP'  THEN 'XRP/USDT'
                    WHEN 'TRX'  THEN 'TRX/USDT'
                END                                                           AS symbol,
                'moex'::text                                                  AS exchange,
                ROUND(m.value_rub::numeric, 2)                                AS volume_rub
            FROM moex_daily_value m
            WHERE m.date >= CURRENT_DATE - INTERVAL '30 days'
              AND m.asset_code NOT IN ('XRP', 'TRX')
        )
        SELECT * FROM crypto_rub
        UNION ALL
        SELECT * FROM moex_rub
        ORDER BY date, symbol, exchange
        """,
        exclude_bases or [],
    )


async def fetch_tradfi_daily_volume() -> list[asyncpg.Record]:
    """
    Daily volume in RUB per tradfi symbol × exchange for the last 30 days.

    Filters crypto to non-crypto bases (Commodities, Precious Metals, US Market).
    MOEX FORTS is all tradfi by definition — included as-is.
    """
    tradfi_bases = [
        'BRN', 'WTI', 'USOIL', 'NATGAS', 'NGAS', 'UKOIL', 'BRENT',
        'COPPER', 'ALUMINIUM', 'WHEAT', 'CORN', 'URANIUM', 'TTF',
        'XAU', 'XAG', 'XPT', 'XPD',
        'NVDA', 'QQQ', 'SPY', 'AAPL', 'TSLA', 'AMZN', 'MSFT', 'GOOGL', 'META', 'SPCX',
    ]
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH crypto_rub AS (
            SELECT
                o.ts::date                                                   AS date,
                to_char(o.ts::date, 'Mon DD')                                AS date_label,
                o.symbol,
                o.exchange,
                ROUND((o.quote_volume * fx.usdrub)::numeric, 2)              AS volume_rub
            FROM ohlcv_daily o
            LEFT JOIN LATERAL (
                SELECT usdrub
                FROM moex_fx_rates
                WHERE date <= o.ts::date
                ORDER BY date DESC
                LIMIT 1
            ) fx ON TRUE
            WHERE o.ts >= (CURRENT_DATE - INTERVAL '30 days')
              AND o.ts <  CURRENT_DATE + INTERVAL '1 day'
              AND fx.usdrub IS NOT NULL
              AND SPLIT_PART(o.symbol, '/', 1) = ANY($1)
        ),
        moex_rub AS (
            SELECT
                m.date,
                to_char(m.date, 'Mon DD')                                    AS date_label,
                CASE m.asset_code
                    WHEN 'BR' THEN 'BRN/USDT:USDT'
                    WHEN 'NG' THEN 'NATGAS/USDT:USDT'
                    WHEN 'GD' THEN 'XAU/USDT:USDT'
                    WHEN 'SV' THEN 'XAG/USDT:USDT'
                    WHEN 'PT' THEN 'XPT/USDT:USDT'
                    WHEN 'PD' THEN 'XPD/USDT:USDT'
                    WHEN 'NASD' THEN 'QQQ/USDT:USDT'
                    WHEN 'SPYF' THEN 'SPY/USDT:USDT'
                    WHEN 'BTC'  THEN 'BTC/USDT'
                    WHEN 'ETH'  THEN 'ETH/USDT'
                    WHEN 'SOL'  THEN 'SOL/USDT'
                    WHEN 'XRP'  THEN 'XRP/USDT'
                    WHEN 'TRX'  THEN 'TRX/USDT'
                END                                                           AS symbol,
                'moex'::text                                                  AS exchange,
                ROUND(m.value_rub::numeric, 2)                                AS volume_rub
            FROM moex_daily_value m
            WHERE m.date >= CURRENT_DATE - INTERVAL '30 days'
              AND m.asset_code NOT IN ('BTC', 'ETH', 'SOL', 'XRP', 'TRX')
        )
        SELECT * FROM crypto_rub
        UNION ALL
        SELECT * FROM moex_rub
        ORDER BY date, symbol, exchange
        """,
        tradfi_bases,
    )


async def fetch_weekly_volume_rub(tradfi_only: bool = False) -> list[asyncpg.Record]:
    """
    Weekly SUMMED trading volume in RUB per symbol × exchange × ISO week, YTD.

    Same USDT→RUB conversion as fetch_weekly_adtv_rub (LEFT JOIN LATERAL on the
    forward-filled USDRUBF rate) but returns the *total* volume for the week
    rather than the average-daily (ADTV).  MOEX FORTS contributes a synthetic
    'moex' exchange row (already in RUB).  When tradfi_only is True, crypto is
    restricted to non-crypto bases (Commodities, Precious Metals, US Market);
    MOEX FORTS is tradfi by definition and always included.
    """
    tradfi_bases = [
        'BRN', 'WTI', 'USOIL', 'NATGAS', 'NGAS', 'UKOIL', 'BRENT',
        'COPPER', 'ALUMINIUM', 'WHEAT', 'CORN', 'URANIUM', 'TTF',
        'XAU', 'XAG', 'XPT', 'XPD',
        'NVDA', 'QQQ', 'SPY', 'AAPL', 'TSLA', 'AMZN', 'MSFT', 'GOOGL', 'META', 'SPCX',
    ]
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH crypto_rub AS (
            SELECT
                date_trunc('week', o.ts)::date                          AS week_start,
                o.symbol,
                o.exchange,
                ROUND(SUM(o.quote_volume * fx.usdrub)::numeric, 2)       AS volume_rub
            FROM ohlcv_daily o
            LEFT JOIN LATERAL (
                SELECT usdrub
                FROM moex_fx_rates
                WHERE date <= o.ts::date
                ORDER BY date DESC
                LIMIT 1
            ) fx ON TRUE
            WHERE o.ts >= GREATEST(DATE '2026-01-12', date_trunc('year', CURRENT_DATE)::date)
              AND o.ts <  CURRENT_DATE + INTERVAL '1 day'
              AND fx.usdrub IS NOT NULL
              AND ($2 = FALSE OR SPLIT_PART(o.symbol, '/', 1) = ANY($1))
            GROUP BY date_trunc('week', o.ts), o.symbol, o.exchange
        ),
        moex_rub AS (
            SELECT
                date_trunc('week', m.date)::date                        AS week_start,
                CASE m.asset_code
                    WHEN 'BR' THEN 'BRN/USDT:USDT'
                    WHEN 'NG' THEN 'NATGAS/USDT:USDT'
                    WHEN 'GD' THEN 'XAU/USDT:USDT'
                    WHEN 'SV' THEN 'XAG/USDT:USDT'
                    WHEN 'PT' THEN 'XPT/USDT:USDT'
                    WHEN 'PD' THEN 'XPD/USDT:USDT'
                    WHEN 'NASD' THEN 'QQQ/USDT:USDT'
                    WHEN 'SPYF' THEN 'SPY/USDT:USDT'
                    WHEN 'BTC'  THEN 'BTC/USDT'
                    WHEN 'ETH'  THEN 'ETH/USDT'
                    WHEN 'SOL'  THEN 'SOL/USDT'
                    WHEN 'XRP'  THEN 'XRP/USDT'
                    WHEN 'TRX'  THEN 'TRX/USDT'
                END                                                      AS symbol,
                'moex'::text                                            AS exchange,
                ROUND(SUM(m.value_rub)::numeric, 2)                     AS volume_rub
            FROM moex_daily_value m
            WHERE m.date >= GREATEST(DATE '2026-01-12', date_trunc('year', CURRENT_DATE)::date)
              AND ($2 = FALSE OR m.asset_code NOT IN ('BTC', 'ETH', 'SOL', 'XRP', 'TRX'))
            GROUP BY date_trunc('week', m.date), m.asset_code
        )
        SELECT * FROM crypto_rub
        UNION ALL
        SELECT * FROM moex_rub
        ORDER BY week_start, symbol, exchange
        """,
        tradfi_bases,
        tradfi_only,
    )


async def fetch_history_metrics_by_exchange() -> list[asyncpg.Record]:
    """Per-exchange ADTV breakdown used for the detail tooltip.

    Upper bound (CURRENT_DATE + 1 day) prevents the planner from locking
    every chunk in the hypertable — including leftover future-dated
    placeholder chunks — which otherwise hits max_locks_per_transaction.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            symbol,
            exchange,
            ROUND(AVG(quote_volume)::numeric, 2) AS adtv_ytd,
            COUNT(*)                              AS ytd_days,
            ROUND(AVG(quote_volume) FILTER (
                WHERE ts >= date_trunc('week', NOW() AT TIME ZONE 'UTC')
            )::numeric, 2)                        AS adtv_week,
            ROUND(AVG(quote_volume) FILTER (
                WHERE ts >= date_trunc('week', (NOW() - INTERVAL '7 days') AT TIME ZONE 'UTC')
                  AND ts <  date_trunc('week', NOW() AT TIME ZONE 'UTC')
            )::numeric, 2)                        AS adtv_last_week
        FROM ohlcv_daily
        WHERE ts >= date_trunc('year', CURRENT_DATE)::date
          AND ts <  CURRENT_DATE + INTERVAL '1 day'
        GROUP BY symbol, exchange
        ORDER BY symbol, exchange
        """
    )


# ── Funding rates ─────────────────────────────────────────────────────────────

async def upsert_funding_rates(rows: list[tuple]) -> int:
    """
    Bulk upsert settled funding rate rows.
    Each tuple: (time, symbol, exchange, rate, interval_hours)
    Returns number of rows upserted.
    """
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO funding_rates (time, symbol, exchange, rate, interval_hours)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (time, symbol, exchange) DO NOTHING
                """,
                rows,
            )
    return len(rows)


async def get_funding_rate_latest_ts(symbol: str, exchange: str):
    """Return the most recent stored settlement timestamp for this symbol × exchange."""
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT MAX(time) FROM funding_rates WHERE symbol = $1 AND exchange = $2",
        symbol, exchange,
    )


async def fetch_latest_funding_rates() -> list[asyncpg.Record]:
    """Latest settled rate per symbol × exchange (for dashboard)."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT DISTINCT ON (symbol, exchange)
            time, symbol, exchange, rate, interval_hours
        FROM funding_rates
        ORDER BY symbol, exchange, time DESC
        """
    )


async def fetch_funding_rate_history_db(
    symbol: str,
    exchange: str | None = None,
    since=None,
    limit: int = 5000,
) -> list[asyncpg.Record]:
    pool = await get_pool()
    conditions = ["symbol = $1"]
    params: list = [symbol]
    p = 2
    if exchange:
        conditions.append(f"exchange = ${p}")
        params.append(exchange)
        p += 1
    if since:
        conditions.append(f"time >= ${p}")
        params.append(since)
        p += 1
    params.append(limit)
    where = " AND ".join(conditions)
    return await pool.fetch(
        f"""
        SELECT time, symbol, exchange, rate, interval_hours
        FROM funding_rates
        WHERE {where}
        ORDER BY time ASC
        LIMIT ${p}
        """,
        *params,
    )


async def fetch_funding_daily(days: int = 30) -> list[asyncpg.Record]:
    """
    Settled funding per day × symbol × exchange — the source of the funding
    heatmap (instruments × dates, like the SPB one).

    `pct_day` sums the day's settlements, so it is what a position actually paid
    that day.  `pct_year` annualises the day's MEAN rate by the venue's funding
    interval, which stays comparable on a day where an exchange settled fewer
    times than usual.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            time::date                                             AS date,
            symbol,
            exchange,
            SUM(rate) * 100                                        AS pct_day,
            AVG(rate) * (8760.0 / NULLIF(AVG(interval_hours), 0)) * 100 AS pct_year,
            COUNT(*)                                               AS settlements
        FROM funding_rates
        WHERE time >= CURRENT_DATE - ($1::int || ' days')::interval
          AND time <  CURRENT_DATE + INTERVAL '1 day'
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
        """,
        days,
    )


async def fetch_funding_symbols() -> list[str]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT symbol FROM funding_rates ORDER BY symbol"
    )
    return [r["symbol"] for r in rows]


# ── MOEX FX rates ─────────────────────────────────────────────────────────────

async def upsert_moex_fx_rates(rows: list[tuple]) -> int:
    """Bulk upsert (date, usdrub) pairs into moex_fx_rates. Returns row count."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO moex_fx_rates (date, usdrub)
                VALUES ($1, $2)
                ON CONFLICT (date) DO UPDATE SET usdrub = EXCLUDED.usdrub
                """,
                rows,
            )
    return len(rows)


async def get_moex_fx_latest_date():
    pool = await get_pool()
    return await pool.fetchval("SELECT MAX(date) FROM moex_fx_rates")


async def get_moex_oi_latest_date():
    """Most recent MOEX open-interest day already stored (None on first run)."""
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT MAX(ts)::date FROM open_interest WHERE exchange = 'moex'"
    )


async def fetch_moex_fx_rates_range(from_date, till_date) -> list:
    pool = await get_pool()
    return await pool.fetch(
        "SELECT date, usdrub FROM moex_fx_rates WHERE date BETWEEN $1 AND $2 ORDER BY date",
        from_date, till_date,
    )


# ── MOEX daily volumes ────────────────────────────────────────────────────────

async def upsert_moex_daily_value(rows: list[tuple]) -> int:
    """Bulk upsert (date, asset_code, value_rub) into moex_daily_value. Returns row count."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO moex_daily_value (date, asset_code, value_rub)
                VALUES ($1, $2, $3)
                ON CONFLICT (date, asset_code) DO UPDATE SET value_rub = EXCLUDED.value_rub
                """,
                rows,
            )
    return len(rows)


async def get_moex_asset_latest_date(asset_code: str):
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT MAX(date) FROM moex_daily_value WHERE asset_code = $1", asset_code
    )


# ── SPB Exchange perp turnover ────────────────────────────────────────────────

async def upsert_spb_daily_volume(rows: list[tuple]) -> int:
    """Bulk upsert (date, ticker, volume, turnover_usd) into spb_daily_volume."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO spb_daily_volume (date, ticker, volume, turnover_usd)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (date, ticker) DO UPDATE SET
                    volume       = EXCLUDED.volume,
                    turnover_usd = EXCLUDED.turnover_usd
                """,
                rows,
            )
    return len(rows)


async def get_spb_latest_date(ticker: str):
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT MAX(date) FROM spb_daily_volume WHERE ticker = $1", ticker
    )


async def fetch_spb_daily_volume() -> list[asyncpg.Record]:
    """
    Daily SPB perp turnover in RUB per ticker for the last 30 days.

    Turnover is stored in USD; converted USD→RUB via the most-recent USDRUBF rate
    (LEFT JOIN LATERAL on moex_fx_rates, forward-filled for weekends/holidays) —
    the same conversion used by the crypto volume pages.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            s.date,
            to_char(s.date, 'Mon DD')                                AS date_label,
            s.ticker,
            ROUND((s.turnover_usd * fx.usdrub)::numeric, 2)          AS turnover_rub
        FROM spb_daily_volume s
        LEFT JOIN LATERAL (
            SELECT usdrub
            FROM moex_fx_rates
            WHERE date <= s.date
            ORDER BY date DESC
            LIMIT 1
        ) fx ON TRUE
        WHERE s.date >= CURRENT_DATE - INTERVAL '30 days'
          AND fx.usdrub IS NOT NULL
        ORDER BY s.date, s.ticker
        """
    )


async def fetch_spb_weekly_adtv() -> list[asyncpg.Record]:
    """
    Weekly ADTV (average daily turnover) in RUB per SPB ticker × ISO week.

    Each day's USD turnover is converted at that day's USDRUBF rate (LEFT JOIN
    LATERAL, forward-filled), summed per ISO week, then divided by the number of
    trading days in the week — the same ADTV definition as the crypto page.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            date_trunc('week', s.date)::date                AS week_start,
            to_char(date_trunc('week', s.date), 'Mon DD')   AS week_label,
            s.ticker,
            COUNT(*)                                        AS days_in_week,
            ROUND(
                (SUM(s.turnover_usd * fx.usdrub) / NULLIF(COUNT(*), 0))::numeric, 2
            )                                               AS adtv
        FROM spb_daily_volume s
        LEFT JOIN LATERAL (
            SELECT usdrub
            FROM moex_fx_rates
            WHERE date <= s.date
            ORDER BY date DESC
            LIMIT 1
        ) fx ON TRUE
        WHERE s.date >= date_trunc('year', CURRENT_DATE)::date
          AND fx.usdrub IS NOT NULL
        GROUP BY date_trunc('week', s.date), s.ticker
        ORDER BY week_start, s.ticker
        """
    )


# ── SPB Exchange open interest ────────────────────────────────────────────────

async def upsert_spb_open_interest(rows: list[tuple]) -> int:
    """Bulk upsert (date, ticker, oi_contracts, oi_usd) into spb_open_interest."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO spb_open_interest (date, ticker, oi_contracts, oi_usd)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (date, ticker) DO UPDATE SET
                    oi_contracts = EXCLUDED.oi_contracts,
                    oi_usd       = EXCLUDED.oi_usd
                """,
                rows,
            )
    return len(rows)


async def get_spb_oi_latest_date():
    """Most recent stored OI date across all tickers (None on an empty table)."""
    pool = await get_pool()
    return await pool.fetchval("SELECT MAX(date) FROM spb_open_interest")


async def fetch_spb_oi_daily() -> list[asyncpg.Record]:
    """
    Daily SPB perp open interest per ticker for the last 30 days.

    oi_usd (open-position notional) is converted USD→RUB via the most-recent
    USDRUBF rate (LEFT JOIN LATERAL on moex_fx_rates, forward-filled) — the same
    conversion as the turnover pages. oi_contracts is passed through as-is.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            s.date,
            to_char(s.date, 'Mon DD')                          AS date_label,
            s.ticker,
            s.oi_contracts,
            ROUND((s.oi_usd * fx.usdrub)::numeric, 2)          AS oi_rub
        FROM spb_open_interest s
        LEFT JOIN LATERAL (
            SELECT usdrub
            FROM moex_fx_rates
            WHERE date <= s.date
            ORDER BY date DESC
            LIMIT 1
        ) fx ON TRUE
        WHERE s.date >= CURRENT_DATE - INTERVAL '30 days'
          AND fx.usdrub IS NOT NULL
        ORDER BY s.date, s.ticker
        """
    )


# ── SPB funding (uploaded from the Telegram CSVs) ─────────────────────────────

async def upsert_spb_funding(rows: list[tuple]) -> int:
    """Bulk upsert (date, ticker, pct_year, pct_day, fund_curr, mean_price,
    mean_index) into spb_funding.  Re-uploading a day overwrites it."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO spb_funding
                    (date, ticker, pct_year, pct_day, fund_curr, mean_price, mean_index)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (date, ticker) DO UPDATE SET
                    pct_year   = EXCLUDED.pct_year,
                    pct_day    = EXCLUDED.pct_day,
                    fund_curr  = EXCLUDED.fund_curr,
                    mean_price = EXCLUDED.mean_price,
                    mean_index = EXCLUDED.mean_index
                """,
                rows,
            )
    return len(rows)


async def upsert_spb_funding_from_exchange(rows: list[tuple]) -> int:
    """
    Upsert rows derived from СПБ Биржа's own funding feed.

    Same shape as ``upsert_spb_funding``, but it refuses to overwrite a row that
    came from a channel CSV: the channel publishes the exchange's exact
    MeanPrice/MeanIndex, while the feed has neither, so our percentages are
    derived from a price base of our own (see app/spb/funding_exchange.py).
    ``mean_index IS NULL`` is what tells the two apart — the feed never fills it.
    """
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO spb_funding
                    (date, ticker, pct_year, pct_day, fund_curr, mean_price, mean_index)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (date, ticker) DO UPDATE SET
                    pct_year   = EXCLUDED.pct_year,
                    pct_day    = EXCLUDED.pct_day,
                    fund_curr  = EXCLUDED.fund_curr,
                    mean_price = EXCLUDED.mean_price
                WHERE spb_funding.mean_index IS NULL
                """,
                rows,
            )
    return len(rows)


async def get_spb_funding_latest_date() -> date | None:
    """Newest day already stored — the Telegram ingester anchors its scan window
    on it (re-scanning a few days back, since a day can be posted late)."""
    pool = await get_pool()
    return await pool.fetchval("SELECT max(date) FROM spb_funding")


async def fetch_spb_funding() -> list[asyncpg.Record]:
    """Every uploaded funding row, all history, ordered by date then ticker.
    Raw values as stored (no fx conversion — funding is a rate, not a turnover)."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT date, ticker, pct_year, pct_day, fund_curr, mean_price, mean_index
        FROM spb_funding
        ORDER BY date, ticker
        """
    )


# ── SPB order-book "spread on volume" (AVG_SPREAD) history ─────────────────────

async def get_latest_usdrub() -> float | None:
    """Most recent USDRUBF rate (None on an empty table).  Used by the spread
    collector to size V/2 in roubles when walking the book."""
    pool = await get_pool()
    val = await pool.fetchval("SELECT usdrub FROM moex_fx_rates ORDER BY date DESC LIMIT 1")
    return float(val) if val is not None else None


async def upsert_spb_spread_buckets(rows: list[tuple]) -> int:
    """Bulk upsert (bucket, ticker, spread_1m_usd, spread_10m_usd, spread_1m_pct,
    spread_10m_pct)."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO spb_orderbook_spread
                    (bucket, ticker, spread_1m_usd, spread_10m_usd, spread_1m_pct, spread_10m_pct)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (bucket, ticker) DO UPDATE SET
                    spread_1m_usd  = EXCLUDED.spread_1m_usd,
                    spread_10m_usd = EXCLUDED.spread_10m_usd,
                    spread_1m_pct  = EXCLUDED.spread_1m_pct,
                    spread_10m_pct = EXCLUDED.spread_10m_pct
                """,
                rows,
            )
    return len(rows)


async def fetch_spb_spread_history(days: int = 7) -> list[asyncpg.Record]:
    """
    15-minute spread-on-volume history per ticker for the last ``days`` days.

    The absolute spread is the raw USD price gap (P_aver_ask − P_aver_bid),
    returned as stored — **no** USD→RUB conversion.  The percentage spread is
    unit-free (absolute / top-of-book mid).  NULL spreads (illiquid — no depth)
    stay NULL so the chart breaks the line.

    Only the 1 млн ₽ columns are selected: the 10 млн line was dropped from the
    page on 14.07.2026, so shipping ``spread_10m_*`` doubled the payload for
    something nothing reads.  The columns stay in the table.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            s.bucket,
            s.ticker,
            ROUND(s.spread_1m_usd::numeric,  5) AS spread_1m_usd,
            ROUND(s.spread_1m_pct::numeric,  4) AS spread_1m_pct
        FROM spb_orderbook_spread s
        WHERE s.bucket >= now() - ($1::int || ' days')::interval
        ORDER BY s.ticker, s.bucket
        """,
        days,
    )


async def upsert_moex_spread_buckets(rows: list[tuple]) -> int:
    """Bulk upsert (bucket, ticker, spread_1m_usd, spread_1m_pct) for the MOEX
    crypto-futures spread overlay."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO moex_orderbook_spread
                    (bucket, ticker, spread_1m_usd, spread_1m_pct)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (bucket, ticker) DO UPDATE SET
                    spread_1m_usd = EXCLUDED.spread_1m_usd,
                    spread_1m_pct = EXCLUDED.spread_1m_pct
                """,
                rows,
            )
    return len(rows)


async def fetch_moex_spread_history(days: int = 7) -> list[asyncpg.Record]:
    """
    15-minute MOEX crypto-futures spread history per (SPB) ticker for the last
    ``days`` days.  Absolute spread is the raw USD price gap (no fx conversion),
    directly comparable to the SPB line on the same chart.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            s.bucket,
            s.ticker,
            ROUND(s.spread_1m_usd::numeric, 5) AS spread_1m_usd,
            ROUND(s.spread_1m_pct::numeric, 4) AS spread_1m_pct
        FROM moex_orderbook_spread s
        WHERE s.bucket >= now() - ($1::int || ' days')::interval
        ORDER BY s.ticker, s.bucket
        """,
        days,
    )


async def upsert_mm_spread_buckets(rows: list[tuple]) -> int:
    """Bulk upsert (bucket, ticker, group_id, spread_abs, spread_pct) for the MM
    FORTS spread history."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO mm_orderbook_spread
                    (bucket, ticker, group_id, spread_abs, spread_pct)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (bucket, ticker) DO UPDATE SET
                    group_id   = EXCLUDED.group_id,
                    spread_abs = EXCLUDED.spread_abs,
                    spread_pct = EXCLUDED.spread_pct
                """,
                rows,
            )
    return len(rows)


async def fetch_mm_spread_history(group_id: str, days: int = 7) -> list[asyncpg.Record]:
    """
    15-minute MM FORTS spread history for one group over the last ``days`` days.
    ``spread_abs`` is returned as stored (the instrument's quote unit — no
    conversion); ``spread_pct`` is unit-free.  NULLs (illiquid — no 1 млн ₽
    depth) stay NULL.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT
            s.bucket,
            s.ticker,
            ROUND(s.spread_abs::numeric, 6) AS spread_abs,
            ROUND(s.spread_pct::numeric, 4) AS spread_pct
        FROM mm_orderbook_spread s
        WHERE s.group_id = $1
          AND s.bucket >= now() - ($2::int || ' days')::interval
        ORDER BY s.ticker, s.bucket
        """,
        group_id,
        days,
    )


# ── Raw order-book snapshots (MM-presence estimator) ─────────────────────────

async def insert_ob_levels(rows: list[tuple]) -> int:
    """Bulk insert (ts, symbol, side, level_idx, price, volume).

    ``DO NOTHING`` on conflict: a collector restart inside a grid point must not
    double-write the snapshot it already stored, and re-writing it with fresh
    levels would silently mix two instants under one timestamp.
    """
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO ob_snapshot_level (ts, symbol, side, level_idx, price, volume)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (ts, symbol, side, level_idx) DO NOTHING
                """,
                rows,
            )
    return len(rows)


async def open_capture_session(symbol: str, step_sec: int) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """
        INSERT INTO ob_capture_session (symbol, started, step_sec)
        VALUES ($1, now(), $2) RETURNING id
        """,
        symbol, step_sec,
    )


async def update_capture_session(session_id: int, n_snapshots: int, n_missed: int,
                                 closed: bool = False) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE ob_capture_session
           SET n_snapshots = $2,
               n_missed    = $3,
               ended       = CASE WHEN $4 THEN now() ELSE ended END
         WHERE id = $1
        """,
        session_id, n_snapshots, n_missed, closed,
    )


async def fetch_ob_snapshots(symbol: str, ts_from, ts_to,
                             stride_sec: int | None = None) -> list[asyncpg.Record]:
    """Raw levels for one instrument in [ts_from, ts_to), ordered so the caller
    can group them into snapshots in one pass.  Both bounds are always given —
    an unbounded scan would lock every chunk of the hypertable (see the chunk
    locking gotcha in CLAUDE.md).

    ``stride_sec`` keeps only grid points divisible by it, which is how the
    all-instrument summary stays interactive: it needs the shape of the record,
    not every 5-second frame of it.  The grid is epoch-aligned, so this thins
    the series evenly instead of favouring any part of the window.
    """
    pool = await get_pool()
    if stride_sec and stride_sec > 1:
        return await pool.fetch(
            """
            SELECT ts, side, level_idx, price, volume
              FROM ob_snapshot_level
             WHERE symbol = $1
               AND ts >= $2
               AND ts <  $3
               AND MOD(EXTRACT(epoch FROM ts)::bigint, $4::bigint) = 0
             ORDER BY ts, side, level_idx
            """,
            symbol, ts_from, ts_to, stride_sec,
        )
    return await pool.fetch(
        """
        SELECT ts, side, level_idx, price, volume
          FROM ob_snapshot_level
         WHERE symbol = $1
           AND ts >= $2
           AND ts <  $3
         ORDER BY ts, side, level_idx
        """,
        symbol, ts_from, ts_to,
    )


async def fetch_ob_coverage(ts_from, ts_to) -> list[asyncpg.Record]:
    """Per instrument: stored snapshots and their span in the window.

    Counts distinct timestamps rather than rows (a thin book contributes fewer
    rows per snapshot, which would otherwise read as worse coverage)."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT symbol,
               COUNT(DISTINCT ts) AS n_snapshots,
               MIN(ts)            AS first_ts,
               MAX(ts)            AS last_ts
          FROM ob_snapshot_level
         WHERE ts >= $1 AND ts < $2
         GROUP BY symbol
        """,
        ts_from, ts_to,
    )


async def fetch_ob_miss_stats(ts_from, ts_to) -> list[asyncpg.Record]:
    """Capture bookkeeping per instrument over the window: how many grid points
    the collector took and how many it had to skip (dead feed / empty book).
    The miss ratio qualifies every persistence number on the page."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT symbol,
               SUM(n_snapshots)::bigint AS n_snapshots,
               SUM(n_missed)::bigint    AS n_missed
          FROM ob_capture_session
         WHERE started < $2
           AND (ended IS NULL OR ended >= $1)
         GROUP BY symbol
        """,
        ts_from, ts_to,
    )


# ── Crypto-exchange equity (stock) perp turnover ──────────────────────────────

async def upsert_stock_daily_volume(rows: list[tuple]) -> int:
    """Bulk upsert (date, exchange, ticker, quote_usd) into stock_daily_volume."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO stock_daily_volume (date, exchange, ticker, quote_usd)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (date, exchange, ticker) DO UPDATE SET
                    quote_usd = EXCLUDED.quote_usd
                """,
                rows,
            )
    return len(rows)


async def upsert_stock_hourly_volume(rows: list[tuple]) -> int:
    """Bulk upsert (hour, exchange, ticker, quote_usd) into stock_hourly_volume."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO stock_hourly_volume (hour, exchange, ticker, quote_usd)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (hour, exchange, ticker) DO UPDATE SET
                    quote_usd = EXCLUDED.quote_usd
                """,
                rows,
            )
    return len(rows)


async def get_stock_hourly_latest_ts(exchange: str | None = None):
    """Most recent stored hourly stock bar (None on an empty table)."""
    pool = await get_pool()
    if exchange is not None:
        return await pool.fetchval(
            "SELECT MAX(hour) FROM stock_hourly_volume WHERE exchange = $1", exchange
        )
    return await pool.fetchval("SELECT MAX(hour) FROM stock_hourly_volume")


async def fetch_stock_hourly_profile(by: str, days: int = 30) -> list[asyncpg.Record]:
    """
    Intraday profile of equity-perp turnover: mean RUB volume per MSK hour-of-day,
    grouped ``by='exchange'`` or ``by='instrument'`` (ticker).

    Two-level aggregate — SUM within a (day, hour, series), then AVG across days.
    Averaging the day totals (rather than averaging each ticker separately and
    summing) is what "the average turnover of this exchange in this hour" means:
    on a day when a ticker was not yet listed the exchange really did trade less,
    so contributing a zero there is correct, not dilution.

    The upper time bound is required — without it the planner locks every chunk in
    the hypertable (see the chunk-locking note in CLAUDE.md).
    """
    key = "s.exchange" if by == "exchange" else "s.ticker"
    pool = await get_pool()
    return await pool.fetch(
        f"""
        WITH per_day AS (
            SELECT
                (s.hour AT TIME ZONE 'Europe/Moscow')::date                  AS msk_day,
                EXTRACT(hour FROM s.hour AT TIME ZONE 'Europe/Moscow')::int  AS hour_msk,
                {key}                                                        AS series,
                SUM(s.quote_usd * fx.usdrub)                                 AS volume_rub
            FROM stock_hourly_volume s
            LEFT JOIN LATERAL (
                SELECT usdrub FROM moex_fx_rates
                WHERE date <= s.hour::date ORDER BY date DESC LIMIT 1
            ) fx ON TRUE
            WHERE s.hour >= date_trunc('hour', NOW()) - ($1::int * INTERVAL '1 day')
              AND s.hour <  date_trunc('hour', NOW()) + INTERVAL '1 hour'
              AND fx.usdrub IS NOT NULL
            GROUP BY msk_day, hour_msk, series
        )
        SELECT
            hour_msk,
            series,
            ROUND(AVG(volume_rub)::numeric, 2) AS volume_rub,
            COUNT(*)                           AS samples
        FROM per_day
        GROUP BY hour_msk, series
        ORDER BY hour_msk, series
        """,
        days,
    )


# ── Top-N crypto perps (Cryptocurrencies slice of the asset-group charts) ─────

async def upsert_crypto_top_daily(rows: list[tuple]) -> int:
    """Bulk upsert (date, exchange, symbol, quote_usd)."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO crypto_top_daily_volume (date, exchange, symbol, quote_usd)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (date, exchange, symbol) DO UPDATE SET
                    quote_usd = EXCLUDED.quote_usd
                """,
                rows,
            )
    return len(rows)


async def upsert_crypto_top_hourly(rows: list[tuple]) -> int:
    """Bulk upsert (hour, exchange, symbol, quote_usd)."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO crypto_top_hourly_volume (hour, exchange, symbol, quote_usd)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (hour, exchange, symbol) DO UPDATE SET
                    quote_usd = EXCLUDED.quote_usd
                """,
                rows,
            )
    return len(rows)


async def get_crypto_top_daily_latest(exchange: str | None = None):
    """Newest stored day (None on an empty table), optionally per venue."""
    pool = await get_pool()
    if exchange is not None:
        return await pool.fetchval(
            "SELECT MAX(date) FROM crypto_top_daily_volume WHERE exchange = $1", exchange
        )
    return await pool.fetchval("SELECT MAX(date) FROM crypto_top_daily_volume")


async def get_crypto_top_hourly_latest(exchange: str | None = None):
    """Newest stored hour (None on an empty table), optionally per venue."""
    pool = await get_pool()
    if exchange is not None:
        return await pool.fetchval(
            "SELECT MAX(hour) FROM crypto_top_hourly_volume WHERE exchange = $1", exchange
        )
    return await pool.fetchval("SELECT MAX(hour) FROM crypto_top_hourly_volume")


_CRYPTO_TOP_FX_JOIN = """
    LEFT JOIN LATERAL (
        SELECT usdrub FROM moex_fx_rates
        WHERE date <= c.date ORDER BY date DESC LIMIT 1
    ) fx ON TRUE
"""


async def fetch_crypto_top_daily(by: str = "exchange") -> list[asyncpg.Record]:
    """Daily top-N crypto turnover in RUB for the last 30 days."""
    key = "c.exchange" if by == "exchange" else "c.symbol"
    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT
            c.date                                              AS bucket,
            to_char(c.date, 'Mon DD')                           AS bucket_label,
            {key}                                               AS series,
            ROUND(SUM(c.quote_usd * fx.usdrub)::numeric, 2)     AS volume_rub
        FROM crypto_top_daily_volume c
        {_CRYPTO_TOP_FX_JOIN}
        WHERE c.date >= CURRENT_DATE - INTERVAL '30 days'
          AND c.date <  CURRENT_DATE + INTERVAL '1 day'
          AND fx.usdrub IS NOT NULL
        GROUP BY c.date, {key}
        ORDER BY c.date, {key}
        """
    )


async def fetch_crypto_top_weekly(by: str = "exchange") -> list[asyncpg.Record]:
    """Weekly (ISO, Monday) SUMMED top-N crypto turnover in RUB, year-to-date."""
    key = "c.exchange" if by == "exchange" else "c.symbol"
    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT
            date_trunc('week', c.date)::date                    AS bucket,
            to_char(date_trunc('week', c.date), 'Mon DD')       AS bucket_label,
            {key}                                               AS series,
            ROUND(SUM(c.quote_usd * fx.usdrub)::numeric, 2)     AS volume_rub
        FROM crypto_top_daily_volume c
        {_CRYPTO_TOP_FX_JOIN}
        WHERE c.date >= date_trunc('year', CURRENT_DATE)::date
          AND c.date <  CURRENT_DATE + INTERVAL '1 day'
          AND fx.usdrub IS NOT NULL
        GROUP BY date_trunc('week', c.date), {key}
        ORDER BY bucket, {key}
        """
    )


async def fetch_crypto_top_hourly_profile(by: str = "exchange", days: int = 30) -> list[asyncpg.Record]:
    """
    Mean RUB turnover per MSK hour-of-day over the last `days` days.

    SUM within a (day, hour, series) then AVG across days — the same two-level
    shape as `fetch_stock_hourly_profile`, and for the same reason: a coin that
    entered the top-100 midway really did add nothing on the earlier days.
    """
    key = "c.exchange" if by == "exchange" else "c.symbol"
    pool = await get_pool()
    return await pool.fetch(
        f"""
        WITH per_day AS (
            SELECT
                (c.hour AT TIME ZONE 'Europe/Moscow')::date                  AS msk_day,
                EXTRACT(hour FROM c.hour AT TIME ZONE 'Europe/Moscow')::int  AS hour_msk,
                {key}                                                        AS series,
                SUM(c.quote_usd * fx.usdrub)                                 AS volume_rub
            FROM crypto_top_hourly_volume c
            LEFT JOIN LATERAL (
                SELECT usdrub FROM moex_fx_rates
                WHERE date <= c.hour::date ORDER BY date DESC LIMIT 1
            ) fx ON TRUE
            WHERE c.hour >= date_trunc('hour', NOW()) - ($1::int * INTERVAL '1 day')
              AND c.hour <  date_trunc('hour', NOW()) + INTERVAL '1 hour'
              AND fx.usdrub IS NOT NULL
            GROUP BY msk_day, hour_msk, series
        )
        SELECT hour_msk, series,
               ROUND(AVG(volume_rub)::numeric, 2) AS volume_rub,
               COUNT(*)                           AS samples
        FROM per_day
        GROUP BY hour_msk, series
        ORDER BY hour_msk, series
        """,
        days,
    )


async def fetch_launch_first_trades() -> dict[tuple[str, str], str]:
    """{(exchange, symbol): 'YYYY-MM-DD'} — derived Launches listing dates."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT exchange, symbol, first_traded::text FROM launch_first_trade"
    )
    return {(r["exchange"], r["symbol"]): r["first_traded"] for r in rows}


async def upsert_launch_first_trades(rows: list[tuple]) -> int:
    """Bulk upsert (exchange, symbol, 'YYYY-MM-DD') into launch_first_trade."""
    if not rows:
        return 0
    # asyncpg binds a DATE parameter as datetime.date — an ISO string is rejected
    # even with an explicit ::date cast in the statement.
    params = [(ex, sym, date.fromisoformat(d) if isinstance(d, str) else d)
              for ex, sym, d in rows]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO launch_first_trade (exchange, symbol, first_traded)
                VALUES ($1, $2, $3)
                ON CONFLICT (exchange, symbol) DO UPDATE SET
                    first_traded = EXCLUDED.first_traded
                """,
                params,
            )
    return len(params)


async def get_stock_latest_date(exchange: str | None = None):
    """Most recent stored stock-volume date (None on an empty table).

    With `exchange` set, restrict to that venue so a newly-added exchange
    backfills its full history rather than inheriting the global latest date.
    """
    pool = await get_pool()
    if exchange is not None:
        return await pool.fetchval(
            "SELECT MAX(date) FROM stock_daily_volume WHERE exchange = $1", exchange
        )
    return await pool.fetchval("SELECT MAX(date) FROM stock_daily_volume")


# Shared USD→RUB conversion (USDRUBF, forward-filled) applied to stock turnover.
_STOCK_FX_JOIN = """
    LEFT JOIN LATERAL (
        SELECT usdrub FROM moex_fx_rates
        WHERE date <= s.date ORDER BY date DESC LIMIT 1
    ) fx ON TRUE
"""


async def fetch_stock_daily_volume(by: str) -> list[asyncpg.Record]:
    """
    Daily stock-perp turnover in RUB for the last 30 days, grouped either
    ``by='exchange'`` or ``by='instrument'`` (ticker).  Crypto USD→RUB via USDRUBF.
    """
    key = "s.exchange" if by == "exchange" else "s.ticker"
    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT
            s.date                                              AS bucket,
            to_char(s.date, 'Mon DD')                           AS bucket_label,
            {key}                                               AS series,
            ROUND(SUM(s.quote_usd * fx.usdrub)::numeric, 2)     AS volume_rub
        FROM stock_daily_volume s
        {_STOCK_FX_JOIN}
        WHERE s.date >= CURRENT_DATE - INTERVAL '30 days'
          AND fx.usdrub IS NOT NULL
        GROUP BY s.date, {key}
        ORDER BY s.date, {key}
        """
    )


async def fetch_stock_weekly_volume(by: str) -> list[asyncpg.Record]:
    """
    Weekly (ISO, Monday) SUMMED stock-perp turnover in RUB, year-to-date,
    grouped ``by='exchange'`` or ``by='instrument'``.
    """
    key = "s.exchange" if by == "exchange" else "s.ticker"
    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT
            date_trunc('week', s.date)::date                    AS bucket,
            to_char(date_trunc('week', s.date), 'Mon DD')       AS bucket_label,
            {key}                                               AS series,
            ROUND(SUM(s.quote_usd * fx.usdrub)::numeric, 2)     AS volume_rub
        FROM stock_daily_volume s
        {_STOCK_FX_JOIN}
        WHERE s.date >= date_trunc('year', CURRENT_DATE)::date
          AND s.date <  CURRENT_DATE + INTERVAL '1 day'
          AND fx.usdrub IS NOT NULL
        GROUP BY date_trunc('week', s.date), {key}
        ORDER BY bucket, {key}
        """
    )


# ── Equity perps on the Weekly/Daily/OI pages ─────────────────────────────────
#
# The curated `instruments` table carries a handful of US stocks, while the stock
# ETL enumerates the FULL equity-perp universe (~200 tickers).  The US Market
# section of Weekly Performance / Daily Volume / Open Interest is driven by the
# stock ETL, capped to the top tickers by weekly turnover — so the curated stock
# rows must be dropped from the ohlcv branch of the volume queries to avoid
# double counting (same methodology as TradFi Market Share).

US_STOCK_CURATED_BASES = ["NVDA", "AAPL", "TSLA", "AMZN", "MSFT", "GOOGL", "META", "SPCX"]


def _non_us_tickers() -> list[str]:
    """
    Tickers the US Market ranking must skip: Korean names (own section) and
    anything the stock universe excludes.  EXCLUDE is consulted here too because
    dropping a ticker from the universe does not remove the rows it already
    wrote — a de-listed ETF like DRAM would otherwise keep its slot for weeks.
    """
    from app.stocks.config import EXCLUDE, KOREAN_TICKERS
    return list(KOREAN_TICKERS) + sorted(EXCLUDE)


def stock_symbol(ticker: str) -> str:
    """'AAPL' → 'AAPL/USDT:USDT' — canonical form used by the charts."""
    return f"{ticker}/USDT:USDT"


async def fetch_top_stock_tickers(limit: int = 10) -> list[str]:
    """
    Top equity-perp tickers by turnover over the LAST COMPLETE ISO week.

    A complete week keeps the ranking stable within the running week (the current
    week's partial data would reshuffle the set every few hours).  On a fresh DB
    with less than a week of history it falls back to the last 7 days present.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT s.ticker
        FROM stock_daily_volume s
        WHERE s.date >= date_trunc('week', CURRENT_DATE)::date - 7
          AND s.date <  date_trunc('week', CURRENT_DATE)::date
          AND s.ticker <> ALL($2::text[])
        GROUP BY s.ticker
        ORDER BY SUM(s.quote_usd) DESC
        LIMIT $1
        """,
        limit, _non_us_tickers(),
    )
    if not rows:
        rows = await pool.fetch(
            """
            SELECT s.ticker
            FROM stock_daily_volume s
            WHERE s.date > (SELECT MAX(date) FROM stock_daily_volume) - 7
              AND s.ticker <> ALL($2::text[])
            GROUP BY s.ticker
            ORDER BY SUM(s.quote_usd) DESC
            LIMIT $1
            """,
            limit, _non_us_tickers(),
        )
    return [r["ticker"] for r in rows]


async def fetch_stock_weekly_adtv_rub(tickers: list[str]) -> list[asyncpg.Record]:
    """Weekly ADTV in RUB per equity ticker × exchange, YTD — same shape as
    ``fetch_weekly_adtv_rub`` so both feed one flat list to the frontend."""
    if not tickers:
        return []
    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT
            date_trunc('week', s.date)::date                     AS week_start,
            to_char(date_trunc('week', s.date), 'Mon DD')        AS week_label,
            s.ticker || '/USDT:USDT'                             AS symbol,
            s.exchange,
            COUNT(*)                                             AS days_in_week,
            ROUND(
                (SUM(s.quote_usd * fx.usdrub) / NULLIF(COUNT(*), 0))::numeric, 2
            )                                                    AS adtv
        FROM stock_daily_volume s
        {_STOCK_FX_JOIN}
        WHERE s.date >= date_trunc('year', CURRENT_DATE)::date
          AND s.date <  CURRENT_DATE + INTERVAL '1 day'
          AND s.ticker = ANY($1::text[])
          AND fx.usdrub IS NOT NULL
        GROUP BY date_trunc('week', s.date), s.ticker, s.exchange
        ORDER BY week_start, symbol, s.exchange
        """,
        tickers,
    )


async def fetch_stock_daily_volume_rub(tickers: list[str]) -> list[asyncpg.Record]:
    """Daily turnover in RUB per equity ticker × exchange for the last 30 days —
    same shape as ``fetch_daily_volume_rub``."""
    if not tickers:
        return []
    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT
            s.date                                               AS date,
            to_char(s.date, 'Mon DD')                            AS date_label,
            s.ticker || '/USDT:USDT'                             AS symbol,
            s.exchange,
            ROUND((s.quote_usd * fx.usdrub)::numeric, 2)         AS volume_rub
        FROM stock_daily_volume s
        {_STOCK_FX_JOIN}
        WHERE s.date >= CURRENT_DATE - INTERVAL '30 days'
          AND s.ticker = ANY($1::text[])
          AND fx.usdrub IS NOT NULL
        ORDER BY s.date, symbol, s.exchange
        """,
        tickers,
    )


# ── Open Interest ─────────────────────────────────────────────────────────────

async def upsert_open_interest(rows: list[tuple]) -> int:
    """
    Bulk upsert open interest rows.
    Each tuple: (ts, exchange, symbol, oi_contracts, oi_usdt)
    Returns number of rows upserted.
    """
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO open_interest (ts, exchange, symbol, oi_contracts, oi_usdt)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (ts, exchange, symbol) DO UPDATE SET
                    oi_contracts = EXCLUDED.oi_contracts,
                    oi_usdt      = EXCLUDED.oi_usdt
                """,
                rows,
            )
    return len(rows)


async def fetch_oi_latest() -> list[asyncpg.Record]:
    """Latest OI snapshot per symbol × exchange."""
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT DISTINCT ON (symbol, exchange)
            ts, exchange, symbol, oi_contracts, oi_usdt
        FROM open_interest
        ORDER BY symbol, exchange, ts DESC
        """
    )


async def fetch_oi_history(
    symbol: str,
    exchange: str | None = None,
    days: int = 30,
) -> list[asyncpg.Record]:
    pool = await get_pool()
    if exchange:
        return await pool.fetch(
            """
            SELECT ts, exchange, symbol, oi_contracts, oi_usdt
            FROM open_interest
            WHERE symbol = $1 AND exchange = $2
              AND ts >= NOW() - ($3::int * INTERVAL '1 day')
            ORDER BY ts ASC
            """,
            symbol, exchange, days,
        )
    return await pool.fetch(
        """
        SELECT ts, exchange, symbol, oi_contracts, oi_usdt
        FROM open_interest
        WHERE symbol = $1
          AND ts >= NOW() - ($2::int * INTERVAL '1 day')
        ORDER BY ts ASC
        """,
        symbol, days,
    )


async def fetch_oi_daily(exclude_bases: list[str] | None = None) -> list[asyncpg.Record]:
    """
    Daily OI per (day, exchange, symbol) for the last 30 days, forward-filled.

    OI is a point-in-time stock, so a day with no snapshot doesn't mean OI
    dropped to zero — the position still exists. Exchanges without an OI history
    API (hyperliquid, mexc) leave hard gaps whenever live polling misses a day
    (e.g. the host slept), which showed up as phantom dips in the stacked bars.
    We carry each exchange×symbol's most recent snapshot forward (LOCF) across
    every day from its first appearance to today. The 40-day lower bound on all
    scans keeps the planner pruning chunks (see the chunk-locking gotcha).

    ``exclude_bases`` drops instruments the page does not display BEFORE any of
    that work happens.  The collector now covers the whole equity-perp universe
    (~940 exchange×symbol pairs) while the chart shows ~35 of them.

    The forward-fill is a **single scan plus window functions**, not a lookup per
    (pair, day).  The old shape ran a LATERAL "latest snapshot ≤ this day"
    subquery for every pair on every day of the grid — ~6 000 index probes, each
    of which had to consider the chunks of a 40-day window, and the endpoint took
    4–6 s.  Here one ``DISTINCT ON`` pass reduces the window to one snapshot per
    (pair, day), and the gaps are carried forward with the standard
    ``count(…) OVER`` grouping trick: ``grp`` increments on every day that has a
    real snapshot, so ``first_value`` inside a grp partition is the most recent
    one.  Same rows, ~1.3 s.  ``grp > 0`` drops the days before a pair's first
    snapshot, matching the old ``WHERE snap.oi_usdt IS NOT NULL``.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH snaps AS (
            SELECT DISTINCT ON (exchange, symbol, d)
                   d, exchange, symbol, oi_contracts, oi_usdt
            FROM (
                -- GREATEST clamps snapshots older than the grid onto its first
                -- day, so a pair that stopped reporting before the window still
                -- carries its last known OI across the chart instead of
                -- vanishing — that is what the 40-day scan is for.
                SELECT GREATEST(ts::date, (CURRENT_DATE - INTERVAL '30 days')::date) AS d,
                       ts, exchange, symbol, oi_contracts, oi_usdt
                FROM open_interest
                WHERE ts >= CURRENT_DATE - INTERVAL '40 days'
                  AND ts <  CURRENT_DATE + INTERVAL '1 day'
                  AND oi_usdt IS NOT NULL
                  AND split_part(symbol, '/', 1) <> ALL($1::text[])
            ) z
            ORDER BY exchange, symbol, d, ts DESC
        ),
        grid_days AS (
            SELECT generate_series(
                (CURRENT_DATE - INTERVAL '30 days')::date,
                CURRENT_DATE,
                INTERVAL '1 day'
            )::date AS d
        ),
        pairs AS (
            SELECT exchange, symbol, MIN(d) AS first_day
            FROM snaps
            GROUP BY exchange, symbol
        ),
        expanded AS (
            SELECT g.d, p.exchange, p.symbol, s.oi_contracts, s.oi_usdt
            FROM grid_days g
            JOIN pairs p ON g.d >= p.first_day
            LEFT JOIN snaps s
              ON s.d = g.d AND s.exchange = p.exchange AND s.symbol = p.symbol
        ),
        grouped AS (
            SELECT *, count(oi_usdt) OVER (PARTITION BY exchange, symbol ORDER BY d) AS grp
            FROM expanded
        ),
        filled AS (
            SELECT d AS date, exchange, symbol,
                   first_value(oi_contracts) OVER w AS oi_contracts,
                   first_value(oi_usdt)      OVER w AS oi_usdt
            FROM grouped
            WHERE grp > 0
            WINDOW w AS (PARTITION BY exchange, symbol, grp ORDER BY d)
        ),
        fx_by_day AS (
            SELECT g.d, fx.usdrub
            FROM grid_days g
            LEFT JOIN LATERAL (
                SELECT usdrub
                FROM moex_fx_rates
                WHERE date <= g.d
                ORDER BY date DESC
                LIMIT 1
            ) fx ON TRUE
        )
        SELECT
            f.date,
            to_char(f.date, 'Mon DD')                         AS date_label,
            f.exchange,
            f.symbol,
            f.oi_contracts,
            f.oi_usdt,
            ROUND((f.oi_usdt * x.usdrub)::numeric, 2)          AS oi_rub
        FROM filled f
        JOIN fx_by_day x ON x.d = f.date
        ORDER BY f.date, f.exchange, f.symbol
        """,
        list(exclude_bases or []),
    )


async def fetch_oi_symbols() -> list[str]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT symbol FROM open_interest ORDER BY symbol"
    )
    return [r["symbol"] for r in rows]


async def fetch_oi_equity_bases(days: int = 40) -> list[str]:
    """
    Equity-perp base assets present in the OI window.

    OI rows come from exactly two places — the curated `instruments` table and
    the stock universe — so anything in the table that is not a curated
    instrument is an equity perp.  Deriving the set from `open_interest` itself
    rather than from `stock_daily_volume` is what catches a freshly listed
    ticker: the OI collector picks it up from the live market scan immediately,
    while the stock ETL writes no volume row until the venue has a completed
    daily candle, so a volume-based list misses it for a day or more.

    MOEX is excluded outright — it is a FORTS turnover source, never an equity
    perp, and its symbols are curated anyway.

    `days` matches the scan window of `fetch_oi_daily` and keeps the planner
    pruning chunks (see the chunk-locking gotcha).
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT split_part(o.symbol, '/', 1) AS base
        FROM open_interest o
        WHERE o.ts >= CURRENT_DATE - ($1::int || ' days')::interval
          AND o.ts <  CURRENT_DATE + INTERVAL '1 day'
          AND o.exchange <> 'moex'
          AND NOT EXISTS (
              SELECT 1 FROM instruments i WHERE i.canonical = o.symbol
          )
        """,
        days,
    )
    return [r["base"] for r in rows]


async def get_oi_latest_ts(symbol: str, exchange: str):
    """
    Return the most recent stored ts for this symbol × exchange pair that has
    a valid USD value and is before today (so live-poll data from the current
    day never blocks gap-filling via the backfill path).
    """
    pool = await get_pool()
    return await pool.fetchval(
        """
        SELECT MAX(ts) FROM open_interest
        WHERE symbol = $1 AND exchange = $2
          AND oi_usdt IS NOT NULL
          AND ts::date < CURRENT_DATE
        """,
        symbol, exchange,
    )


async def seed_instruments_from_config():
    """
    Populate the instruments table from ARBI_SYMBOLS env-var on first run.
    No-op if the table already has rows.
    """
    pool = await get_pool()
    count = await pool.fetchval("SELECT count(*) FROM instruments")
    if count:
        return
    for sym in settings.symbols:
        type_  = "perp" if ":" in sym else "spot"
        base   = sym.split("/")[0]
        quote  = sym.split("/")[1].split(":")[0] if "/" in sym else "USDT"
        await pool.execute(
            """
            INSERT INTO instruments (canonical, type, base_asset, quote_asset)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING
            """,
            sym, type_, base, quote,
        )


# ── Custom Reports (ad-hoc report builder) ────────────────────────────────────
#
# A single generic query layer behind the Custom Report page.  Instruments live
# in disjoint tables per metric, so `metric` dispatches to the right source(s):
#   volume        → ohlcv_daily + moex_daily_value + spb_daily_volume + stock_daily_volume
#   open_interest → open_interest + spb_open_interest
#   price         → ohlcv_daily (crypto/tradfi only — SPB/stocks store turnover, not price)
#   funding       → funding_rates (crypto only)
# All money values are RUB by default (comparable across sources via the same
# forward-filled USDRUBF LEFT JOIN LATERAL used everywhere else); currency='usd'
# returns the native USD figure.  Every query bounds the upper time so the
# planner prunes empty future chunks (see max_locks_per_transaction gotcha).

REPORT_METRICS = ("volume", "open_interest", "price", "funding")

# FORTS asset_code → canonical symbol (mirrors the CASE blocks used by the
# weekly/daily volume queries; kept local to the report layer).
_MOEX_CASE = """
    CASE m.asset_code
        WHEN 'BR'   THEN 'BRN/USDT:USDT'
        WHEN 'NG'   THEN 'NATGAS/USDT:USDT'
        WHEN 'GD'   THEN 'XAU/USDT:USDT'
        WHEN 'SV'   THEN 'XAG/USDT:USDT'
        WHEN 'PT'   THEN 'XPT/USDT:USDT'
        WHEN 'PD'   THEN 'XPD/USDT:USDT'
        WHEN 'NASD' THEN 'QQQ/USDT:USDT'
        WHEN 'SPYF' THEN 'SPY/USDT:USDT'
        WHEN 'BTC'  THEN 'BTC/USDT'
        WHEN 'ETH'  THEN 'ETH/USDT'
        WHEN 'SOL'  THEN 'SOL/USDT'
        WHEN 'XRP'  THEN 'XRP/USDT'
        WHEN 'TRX'  THEN 'TRX/USDT'
    END
"""

_FX_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT usdrub FROM moex_fx_rates
        WHERE date <= {col} ORDER BY date DESC LIMIT 1
    ) fx ON TRUE
"""


def _bucket_expr(col: str, agg: str) -> str:
    """Daily → the date itself; weekly → Monday of the ISO week; monthly → 1st of month."""
    if agg == "weekly":
        return f"date_trunc('week', {col})::date"
    if agg == "monthly":
        return f"date_trunc('month', {col})::date"
    return f"{col}::date"


def _label_fmt(agg: str) -> str:
    """Postgres to_char format for the bucket label ('Mon YYYY' for monthly)."""
    return "Mon YYYY" if agg == "monthly" else "Mon DD"


async def fetch_report_options(metric: str) -> dict:
    """
    Return the pickable instruments / exchanges and the available date range for
    a given metric, so the frontend can populate its selectors with only what
    actually has data for that metric.
    """
    if metric not in REPORT_METRICS:
        raise ValueError(f"unknown metric: {metric}")
    pool = await get_pool()
    bound = "ts < CURRENT_DATE + INTERVAL '1 day'"

    if metric == "volume":
        instruments = await pool.fetch(
            f"""
            SELECT DISTINCT symbol FROM (
                SELECT symbol FROM ohlcv_daily WHERE {bound}
                UNION SELECT {_MOEX_CASE} AS symbol FROM moex_daily_value m
                UNION SELECT ticker AS symbol FROM spb_daily_volume
                UNION SELECT ticker AS symbol FROM stock_daily_volume
            ) u WHERE symbol IS NOT NULL ORDER BY symbol
            """
        )
        exchanges = await pool.fetch(
            f"""
            SELECT DISTINCT exchange FROM (
                SELECT exchange FROM ohlcv_daily WHERE {bound}
                UNION SELECT 'moex' UNION SELECT 'spb'
                UNION SELECT exchange FROM stock_daily_volume
            ) u ORDER BY exchange
            """
        )
        rng = await pool.fetchrow(
            """
            SELECT MIN(d) AS lo, MAX(d) AS hi FROM (
                SELECT ts::date AS d FROM ohlcv_daily WHERE ts < CURRENT_DATE + INTERVAL '1 day'
                UNION ALL SELECT date FROM moex_daily_value
                UNION ALL SELECT date FROM spb_daily_volume
                UNION ALL SELECT date FROM stock_daily_volume
            ) u
            """
        )

    elif metric == "open_interest":
        instruments = await pool.fetch(
            f"""
            SELECT DISTINCT symbol FROM (
                SELECT symbol FROM open_interest WHERE {bound}
                UNION SELECT ticker AS symbol FROM spb_open_interest
            ) u ORDER BY symbol
            """
        )
        exchanges = await pool.fetch(
            f"""
            SELECT DISTINCT exchange FROM (
                SELECT exchange FROM open_interest WHERE {bound}
                UNION SELECT 'spb'
            ) u ORDER BY exchange
            """
        )
        rng = await pool.fetchrow(
            """
            SELECT MIN(d) AS lo, MAX(d) AS hi FROM (
                SELECT ts::date AS d FROM open_interest WHERE ts < CURRENT_DATE + INTERVAL '1 day'
                UNION ALL SELECT date FROM spb_open_interest
            ) u
            """
        )

    elif metric == "price":
        instruments = await pool.fetch(
            f"SELECT DISTINCT symbol FROM ohlcv_daily WHERE {bound} ORDER BY symbol"
        )
        exchanges = await pool.fetch(
            f"SELECT DISTINCT exchange FROM ohlcv_daily WHERE {bound} ORDER BY exchange"
        )
        rng = await pool.fetchrow(
            "SELECT MIN(ts::date) AS lo, MAX(ts::date) AS hi FROM ohlcv_daily "
            "WHERE ts < CURRENT_DATE + INTERVAL '1 day'"
        )

    else:  # funding
        instruments = await pool.fetch(
            "SELECT DISTINCT symbol FROM funding_rates ORDER BY symbol"
        )
        exchanges = await pool.fetch(
            "SELECT DISTINCT exchange FROM funding_rates ORDER BY exchange"
        )
        rng = await pool.fetchrow(
            "SELECT MIN(time::date) AS lo, MAX(time::date) AS hi FROM funding_rates"
        )

    return {
        "metric":      metric,
        "instruments": [r["symbol"] for r in instruments],
        "exchanges":   [r["exchange"] for r in exchanges],
        "date_min":    str(rng["lo"]) if rng and rng["lo"] else None,
        "date_max":    str(rng["hi"]) if rng and rng["hi"] else None,
    }


# ── Instrument tree (hierarchical picker: source → exchange → asset class) ─────

_CRYPTO_EXCHANGES = ["binance", "bybit", "hyperliquid", "mexc", "okx", "bitget"]
_CRYPTO_EXCHANGE_LABEL = {
    "binance": "Binance", "bybit": "Bybit", "hyperliquid": "Hyperliquid",
    "mexc": "Mexc", "okx": "OKX", "bitget": "Bitget",
}
_CRYPTO_CLASS_ORDER = ["Crypto", "Commodities", "US stocks", "Indexes", "Korean market"]

_CLS_COMMODITY = {"BRN", "WTI", "USOIL", "NGAS", "NATGAS", "UKOIL", "BRENT",
                  "COPPER", "ALUMINIUM", "WHEAT", "CORN", "URANIUM", "TTF",
                  "XAU", "XAG", "XPT", "XPD"}
_CLS_INDEX     = {"QQQ", "SPY"}
_CLS_KOREAN    = {"SKHYNIX", "SAMSUNG", "HYUNDAI", "SKHY"}   # SKHY = SK Hynix ADR
_CLS_US_STOCK  = {"NVDA", "AAPL", "TSLA", "AMZN", "MSFT", "GOOGL", "META", "SPCX"}

# MOEX FORTS asset_code → (canonical symbol, asset class).
_MOEX_TREE_MAP = {
    "BR": ("BRN/USDT:USDT", "Commodities"), "NG": ("NATGAS/USDT:USDT", "Commodities"),
    "GD": ("XAU/USDT:USDT", "Commodities"), "SV": ("XAG/USDT:USDT", "Commodities"),
    "PT": ("XPT/USDT:USDT", "Commodities"), "PD": ("XPD/USDT:USDT", "Commodities"),
    "NASD": ("QQQ/USDT:USDT", "Indexes"),   "SPYF": ("SPY/USDT:USDT", "Indexes"),
    "BTC": ("BTC/USDT", "Crypto"),  "ETH": ("ETH/USDT", "Crypto"),
    "SOL": ("SOL/USDT", "Crypto"),  "XRP": ("XRP/USDT", "Crypto"),
    "TRX": ("TRX/USDT", "Crypto"),
}


def _fmt_symbol(sym: str) -> str:
    """Match the frontend formatSymbol: 'XAU/USDT:USDT' → 'XAU/USDT PERP'."""
    if ":" not in sym:
        return sym
    return f"{sym.split('/')[0]}/USDT PERP"


def _crypto_class(sym: str, equity: set[str] | None = None) -> str:
    """
    Classify a crypto-exchange instrument into an asset class.

    `equity` carries the tickers the stock ETL knows about — open-interest rows
    name them like any perp ('AVGO/USDT:USDT'), so without it the whole equity
    universe would land in the Crypto branch of the report tree.
    """
    if "/" not in sym:          # equity-perp ticker from stock_daily_volume
        return "US stocks"
    base = sym.split("/")[0]
    if base in _CLS_COMMODITY: return "Commodities"
    if base in _CLS_INDEX:     return "Indexes"
    if base in _CLS_KOREAN:    return "Korean market"
    if base in _CLS_US_STOCK:  return "US stocks"
    if equity and base in equity: return "US stocks"
    return "Crypto"


async def fetch_report_tree(metric: str) -> list[dict]:
    """
    Build the hierarchical instrument picker for a metric:

      Cryptoexchanges → exchange (Binance/Bybit/…) → asset class → instruments
      SPB futures     → asset class (US Market / Crypto) → instruments
      MOEX forts      → asset class (Commodities / Indexes) → instruments

    Leaves carry {symbol, exchange, label}; the frontend selects (exchange,
    symbol) pairs.  Sources absent for a metric (e.g. MOEX has no price/funding)
    are omitted.
    """
    if metric not in REPORT_METRICS:
        raise ValueError(f"unknown metric: {metric}")
    from app.spb.config import SPB_GROUPS, SPB_GROUP_ORDER

    pool = await get_pool()
    bound = "ts < CURRENT_DATE + INTERVAL '1 day'"
    groups: list[dict] = []

    # ── Cryptoexchanges (all metrics) ────────────────────────────────────────
    if metric == "volume":
        rows = await pool.fetch(
            f"SELECT DISTINCT symbol, exchange FROM ohlcv_daily WHERE {bound} "
            "UNION SELECT ticker AS symbol, exchange FROM stock_daily_volume"
        )
    elif metric == "open_interest":
        rows = await pool.fetch(f"SELECT DISTINCT symbol, exchange FROM open_interest WHERE {bound}")
    elif metric == "price":
        rows = await pool.fetch(f"SELECT DISTINCT symbol, exchange FROM ohlcv_daily WHERE {bound}")
    else:  # funding
        rows = await pool.fetch("SELECT DISTINCT symbol, exchange FROM funding_rates")

    # Equity tickers are needed to route stock perps out of the Crypto branch.
    equity = {r["ticker"] for r in await pool.fetch("SELECT DISTINCT ticker FROM stock_daily_volume")}

    by_ex: dict[str, dict[str, set]] = {}
    for r in rows:
        ex = r["exchange"]
        if ex not in _CRYPTO_EXCHANGES:
            continue
        by_ex.setdefault(ex, {}).setdefault(_crypto_class(r["symbol"], equity), set()).add(r["symbol"])

    ex_children = []
    for ex in _CRYPTO_EXCHANGES:
        classes = by_ex.get(ex)
        if not classes:
            continue
        cls_children = []
        for cls in _CRYPTO_CLASS_ORDER:
            syms = sorted(classes.get(cls, set()))
            if not syms:
                continue
            cls_children.append({
                "id": f"{ex}/{cls}", "label": cls,
                "instruments": [{"symbol": s, "exchange": ex, "label": _fmt_symbol(s)} for s in syms],
            })
        if cls_children:
            ex_children.append({"id": ex, "label": _CRYPTO_EXCHANGE_LABEL[ex], "children": cls_children})
    if ex_children:
        groups.append({"id": "crypto", "label": "Cryptoexchanges", "children": ex_children})

    # ── SPB futures (volume + open interest) ─────────────────────────────────
    if metric in ("volume", "open_interest"):
        table = "spb_daily_volume" if metric == "volume" else "spb_open_interest"
        srows = await pool.fetch(f"SELECT DISTINCT ticker FROM {table}")
        by_cls: dict[str, set] = {}
        for r in srows:
            by_cls.setdefault(SPB_GROUPS.get(r["ticker"], "Crypto"), set()).add(r["ticker"])
        cls_children = []
        for cls in SPB_GROUP_ORDER:
            syms = sorted(by_cls.get(cls, set()))
            if not syms:
                continue
            cls_children.append({
                "id": f"spb/{cls}", "label": cls,
                "instruments": [{"symbol": s, "exchange": "spb", "label": s} for s in syms],
            })
        if cls_children:
            groups.append({"id": "spb", "label": "SPB futures", "children": cls_children})

    # ── MOEX forts (volume + open interest) ──────────────────────────────────
    if metric in ("volume", "open_interest"):
        by_cls = {}
        if metric == "volume":
            mrows = await pool.fetch("SELECT DISTINCT asset_code FROM moex_daily_value")
            mapped_rows = [_MOEX_TREE_MAP.get(r["asset_code"]) for r in mrows]
        else:
            # OI is stored per canonical symbol in `open_interest`, not per asset code.
            mrows = await pool.fetch(
                f"SELECT DISTINCT symbol FROM open_interest WHERE exchange = 'moex' AND {bound}"
            )
            by_canon = {canon: cls for canon, cls in _MOEX_TREE_MAP.values()}
            mapped_rows = [
                (r["symbol"], by_canon[r["symbol"]]) if r["symbol"] in by_canon else None
                for r in mrows
            ]
        for mapped in mapped_rows:
            if not mapped:
                continue
            canon, cls = mapped
            by_cls.setdefault(cls, set()).add(canon)
        cls_children = []
        for cls in ["Commodities", "Crypto", "Indexes"]:
            syms = sorted(by_cls.get(cls, set()))
            if not syms:
                continue
            cls_children.append({
                "id": f"moex/{cls}", "label": cls,
                "instruments": [{"symbol": s, "exchange": "moex", "label": _fmt_symbol(s)} for s in syms],
            })
        if cls_children:
            groups.append({"id": "moex", "label": "MOEX forts", "children": cls_children})

    return groups


async def fetch_report(
    metric: str,
    symbols: list[str],
    exchanges: list[str] | None,
    date_from,
    date_to,
    agg: str = "daily",
    currency: str = "rub",
) -> list[asyncpg.Record]:
    """
    Unified report query.  Returns rows of {bucket, bucket_label, symbol,
    exchange, value} for the requested metric / instruments / exchanges / range.

    agg:      'daily' | 'weekly' | 'monthly'
    currency: 'rub'   | 'usd'     (money metrics only; ignored for price/funding)
    """
    if metric not in REPORT_METRICS:
        raise ValueError(f"unknown metric: {metric}")
    if agg not in ("daily", "weekly", "monthly"):
        raise ValueError(f"unknown agg: {agg}")
    if not symbols:
        return []

    pool = await get_pool()
    # $1 from, $2 to, $3 symbols[], $4 exchanges[] (nullable)
    params = [date_from, date_to, symbols, exchanges]
    ex_filter = "($4::text[] IS NULL OR exchange = ANY($4))"
    lf = _label_fmt(agg)
    rub = currency == "rub"

    if metric == "volume":
        b = _bucket_expr("d", agg)
        # Per-source value: crypto/spb/stock stored in USD, moex native RUB.
        crypto_val = "o.quote_volume * fx.usdrub" if rub else "o.quote_volume"
        moex_val   = "m.value_rub" if rub else "m.value_rub / fx.usdrub"
        spb_val    = "s.turnover_usd * fx.usdrub" if rub else "s.turnover_usd"
        stock_val  = "st.quote_usd * fx.usdrub" if rub else "st.quote_usd"
        # fx needed for: crypto/spb/stock when rub; moex when usd.
        crypto_fx = "AND fx.usdrub IS NOT NULL" if rub else ""
        moex_fx   = "" if rub else "AND fx.usdrub IS NOT NULL"
        sql = f"""
        WITH unified AS (
            SELECT o.ts::date AS d, o.symbol, o.exchange, ({crypto_val}) AS val
            FROM ohlcv_daily o
            {_FX_LATERAL.format(col="o.ts::date")}
            WHERE o.ts >= $1 AND o.ts < $2::date + INTERVAL '1 day' {crypto_fx}

            UNION ALL
            SELECT m.date AS d, {_MOEX_CASE} AS symbol, 'moex'::text AS exchange, ({moex_val}) AS val
            FROM moex_daily_value m
            {_FX_LATERAL.format(col="m.date")}
            WHERE m.date >= $1 AND m.date < $2::date + INTERVAL '1 day' {moex_fx}

            UNION ALL
            SELECT s.date AS d, s.ticker AS symbol, 'spb'::text AS exchange, ({spb_val}) AS val
            FROM spb_daily_volume s
            {_FX_LATERAL.format(col="s.date")}
            WHERE s.date >= $1 AND s.date < $2::date + INTERVAL '1 day' {crypto_fx}

            UNION ALL
            SELECT st.date AS d, st.ticker AS symbol, st.exchange, ({stock_val}) AS val
            FROM stock_daily_volume st
            {_FX_LATERAL.format(col="st.date")}
            WHERE st.date >= $1 AND st.date < $2::date + INTERVAL '1 day' {crypto_fx}
        )
        SELECT {b} AS bucket, to_char({b}, '{lf}') AS bucket_label,
               symbol, exchange, ROUND(SUM(val)::numeric, 2) AS value
        FROM unified
        WHERE symbol = ANY($3) AND {ex_filter}
        GROUP BY {b}, symbol, exchange
        ORDER BY bucket, symbol, exchange
        """

    elif metric == "open_interest":
        b = _bucket_expr("d", agg)
        oi_val  = "oi.oi_usdt * fx.usdrub" if rub else "oi.oi_usdt"
        spb_val = "s.oi_usd * fx.usdrub" if rub else "s.oi_usd"
        fx_ok = "AND fx.usdrub IS NOT NULL" if rub else ""
        # OI is a stock, not a flow: take the LAST snapshot within each bucket.
        sql = f"""
        WITH unified AS (
            SELECT oi.ts::date AS d, oi.symbol, oi.exchange, oi.ts AS snap, ({oi_val}) AS val
            FROM open_interest oi
            {_FX_LATERAL.format(col="oi.ts::date")}
            WHERE oi.ts >= $1 AND oi.ts < $2::date + INTERVAL '1 day'
              AND oi.oi_usdt IS NOT NULL {fx_ok}

            UNION ALL
            SELECT s.date AS d, s.ticker AS symbol, 'spb'::text AS exchange,
                   s.date::timestamptz AS snap, ({spb_val}) AS val
            FROM spb_open_interest s
            {_FX_LATERAL.format(col="s.date")}
            WHERE s.date >= $1 AND s.date < $2::date + INTERVAL '1 day' {fx_ok}
        ),
        picked AS (
            SELECT DISTINCT ON ({b}, symbol, exchange)
                   {b} AS bucket, symbol, exchange, val
            FROM unified
            WHERE symbol = ANY($3) AND {ex_filter}
            ORDER BY {b}, symbol, exchange, snap DESC
        )
        SELECT bucket, to_char(bucket, '{lf}') AS bucket_label,
               symbol, exchange, ROUND(val::numeric, 2) AS value
        FROM picked
        ORDER BY bucket, symbol, exchange
        """

    elif metric == "price":
        b = _bucket_expr("ts", agg)
        sql = f"""
        SELECT {b} AS bucket, to_char({b}, '{lf}') AS bucket_label,
               symbol, exchange, ROUND(AVG(close)::numeric, 6) AS value
        FROM ohlcv_daily
        WHERE ts >= $1 AND ts < $2::date + INTERVAL '1 day'
          AND symbol = ANY($3) AND {ex_filter}
        GROUP BY {b}, symbol, exchange
        ORDER BY bucket, symbol, exchange
        """

    else:  # funding
        b = _bucket_expr("time", agg)
        sql = f"""
        SELECT {b} AS bucket, to_char({b}, '{lf}') AS bucket_label,
               symbol, exchange, ROUND(AVG(rate)::numeric, 8) AS value
        FROM funding_rates
        WHERE time >= $1 AND time < $2::date + INTERVAL '1 day'
          AND symbol = ANY($3) AND {ex_filter}
        GROUP BY {b}, symbol, exchange
        ORDER BY bucket, symbol, exchange
        """

    async def _run(syms, dfrom, dto):
        return await pool.fetch(sql, dfrom, dto, syms, exchanges)

    rows = await _run(symbols, date_from, date_to)

    # Fallback: an instrument launched (or delisted) outside the selected window
    # returns nothing above.  Rather than silently dropping it, surface its most
    # recent available data point (bucketed the same way) so the report shows the
    # latest data instead of an empty series.
    present = {r["symbol"] for r in rows}
    missing = [s for s in symbols if s not in present]
    if missing:
        tail = await _run(missing, date(2000, 1, 1), date.today())
        latest: dict[tuple, asyncpg.Record] = {}
        for r in tail:  # ordered by bucket ASC → last write per series is newest
            latest[(r["symbol"], r["exchange"])] = r
        rows = list(rows) + list(latest.values())

    return rows


# ── OKR: MOEX mirror contracts vs the crypto exchanges' TradFi ────────────────

async def upsert_okr_moex_daily(rows: list[tuple]) -> int:
    """Bulk upsert (date, asset_code, value_rub) into okr_moex_daily."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO okr_moex_daily (date, asset_code, value_rub)
                VALUES ($1, $2, $3)
                ON CONFLICT (date, asset_code) DO UPDATE SET value_rub = EXCLUDED.value_rub
                """,
                rows,
            )
    return len(rows)


async def get_okr_stored_dates(since) -> set:
    """Dates already swept, so a backfill doesn't re-request days it has."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT date FROM okr_moex_daily WHERE date >= $1", since
    )
    return {r["date"] for r in rows}


async def fetch_okr_ratio(days: int, assets: list[str], bases: list[str]) -> list[asyncpg.Record]:
    """
    Daily OKR series: MOEX basket turnover, crypto TradFi turnover, and the ratio.

    Numerator   — okr_moex_daily rows whose ASSETCODE is in ``assets`` (already ₽).
    Denominator — the equity-perp universe (stock_daily_volume, all six venues)
                  plus the curated commodity / metal / index perps of ohlcv_daily
                  whose base is in ``bases``.  Both are USD and convert through
                  the same forward-filled USDRUBF join as every other page.

    Only complete days are returned: today is dropped because the crypto side
    trades round the clock while FORTS closes at 23:50 MSK, so a running day
    would report a ratio that merely creeps up over the evening.  Days FORTS did
    not trade never enter the series — an INNER JOIN on the MOEX side keeps
    weekends out instead of drawing them as zero.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        WITH moex AS (
            SELECT date, SUM(value_rub) AS moex_rub
            FROM okr_moex_daily
            WHERE date >= CURRENT_DATE - ($1::int * INTERVAL '1 day')
              AND date <  CURRENT_DATE
              AND asset_code = ANY($2)
            GROUP BY date
        ),
        stocks AS (
            SELECT s.date, SUM(s.quote_usd * fx.usdrub) AS rub
            FROM stock_daily_volume s
            LEFT JOIN LATERAL (
                SELECT usdrub FROM moex_fx_rates
                WHERE date <= s.date ORDER BY date DESC LIMIT 1
            ) fx ON TRUE
            WHERE s.date >= CURRENT_DATE - ($1::int * INTERVAL '1 day')
              AND s.date <  CURRENT_DATE
              AND fx.usdrub IS NOT NULL
            GROUP BY s.date
        ),
        commodities AS (
            SELECT o.ts::date AS date, SUM(o.quote_volume * fx.usdrub) AS rub
            FROM ohlcv_daily o
            LEFT JOIN LATERAL (
                SELECT usdrub FROM moex_fx_rates
                WHERE date <= o.ts::date ORDER BY date DESC LIMIT 1
            ) fx ON TRUE
            WHERE o.ts >= CURRENT_DATE - ($1::int * INTERVAL '1 day')
              AND o.ts <  CURRENT_DATE
              AND o.exchange <> 'moex'
              AND fx.usdrub IS NOT NULL
              AND SPLIT_PART(o.symbol, '/', 1) = ANY($3)
            GROUP BY o.ts::date
        )
        SELECT
            m.date,
            to_char(m.date, 'Mon DD')                       AS date_label,
            ROUND(m.moex_rub::numeric, 2)                   AS moex_rub,
            ROUND((COALESCE(s.rub, 0) + COALESCE(c.rub, 0))::numeric, 2) AS crypto_rub
        FROM moex m
        LEFT JOIN stocks      s ON s.date = m.date
        LEFT JOIN commodities c ON c.date = m.date
        WHERE COALESCE(s.rub, 0) + COALESCE(c.rub, 0) > 0
        ORDER BY m.date
        """,
        days, assets, bases,
    )
