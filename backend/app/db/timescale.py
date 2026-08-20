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


async def fetch_weekly_adtv_rub() -> list[asyncpg.Record]:
    """
    Weekly ADTV in RUB per symbol × exchange × ISO week.

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
            GROUP BY date_trunc('week', m.date), m.asset_code
        )
        SELECT * FROM crypto_rub
        UNION ALL
        SELECT * FROM moex_rub
        ORDER BY week_start, symbol, exchange
        """
    )


async def fetch_daily_volume_rub() -> list[asyncpg.Record]:
    """
    Daily volume in RUB per symbol × exchange for the last 30 days.

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
        )
        SELECT * FROM crypto_rub
        UNION ALL
        SELECT * FROM moex_rub
        ORDER BY date, symbol, exchange
        """
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


async def get_stock_latest_date():
    """Most recent stored stock-volume date (None on an empty table)."""
    pool = await get_pool()
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


async def fetch_oi_daily() -> list[asyncpg.Record]:
    """
    Last OI snapshot per (day, exchange, symbol) for the last 30 days.
    Used by the daily bar charts on the Open Interest page.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT DISTINCT ON (oi.ts::date, oi.exchange, oi.symbol)
            oi.ts::date                                       AS date,
            to_char(oi.ts::date, 'Mon DD')                    AS date_label,
            oi.exchange,
            oi.symbol,
            oi.oi_contracts,
            oi.oi_usdt,
            ROUND((oi.oi_usdt * fx.usdrub)::numeric, 2)       AS oi_rub
        FROM open_interest oi
        LEFT JOIN LATERAL (
            SELECT usdrub
            FROM moex_fx_rates
            WHERE date <= oi.ts::date
            ORDER BY date DESC
            LIMIT 1
        ) fx ON TRUE
        WHERE oi.ts >= CURRENT_DATE - INTERVAL '30 days'
          AND oi.ts <  CURRENT_DATE + INTERVAL '1 day'
          AND oi.oi_usdt IS NOT NULL
        ORDER BY oi.ts::date, oi.exchange, oi.symbol, oi.ts DESC
        """
    )


async def fetch_oi_symbols() -> list[str]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT symbol FROM open_interest ORDER BY symbol"
    )
    return [r["symbol"] for r in rows]


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

_CRYPTO_EXCHANGES = ["binance", "bybit", "hyperliquid", "mexc", "okx"]
_CRYPTO_EXCHANGE_LABEL = {
    "binance": "Binance", "bybit": "Bybit", "hyperliquid": "Hyperliquid",
    "mexc": "Mexc", "okx": "OKX",
}
_CRYPTO_CLASS_ORDER = ["Crypto", "Commodities", "US stocks", "Indexes", "Korean market"]

_CLS_COMMODITY = {"BRN", "WTI", "USOIL", "NGAS", "NATGAS", "UKOIL", "BRENT",
                  "COPPER", "ALUMINIUM", "WHEAT", "CORN", "URANIUM", "TTF",
                  "XAU", "XAG", "XPT", "XPD"}
_CLS_INDEX     = {"QQQ", "SPY"}
_CLS_KOREAN    = {"SKHYNIX", "SAMSUNG", "HYUNDAI"}
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


def _crypto_class(sym: str) -> str:
    """Classify a crypto-exchange instrument into an asset class."""
    if "/" not in sym:          # equity-perp ticker from stock_daily_volume
        return "US stocks"
    base = sym.split("/")[0]
    if base in _CLS_COMMODITY: return "Commodities"
    if base in _CLS_INDEX:     return "Indexes"
    if base in _CLS_KOREAN:    return "Korean market"
    if base in _CLS_US_STOCK:  return "US stocks"
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

    by_ex: dict[str, dict[str, set]] = {}
    for r in rows:
        ex = r["exchange"]
        if ex not in _CRYPTO_EXCHANGES:
            continue
        by_ex.setdefault(ex, {}).setdefault(_crypto_class(r["symbol"]), set()).add(r["symbol"])

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

    # ── MOEX forts (volume only) ─────────────────────────────────────────────
    if metric == "volume":
        mrows = await pool.fetch("SELECT DISTINCT asset_code FROM moex_daily_value")
        by_cls = {}
        for r in mrows:
            mapped = _MOEX_TREE_MAP.get(r["asset_code"])
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
