import json
import re
import asyncpg
from datetime import timedelta
from app.config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
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
            except Exception:
                pass  # view already exists

            try:
                await conn3.execute("""
                    SELECT add_continuous_aggregate_policy('ohlcv_1m',
                        start_offset      => INTERVAL '7 days',
                        end_offset        => INTERVAL '1 minute',
                        schedule_interval => INTERVAL '1 minute',
                        if_not_exists     => TRUE
                    )
                """)
            except Exception:
                pass

        # ── Funding rates (settled, for backtesting) ─────────────────────────────
        # Entire block is idempotent — all errors are swallowed so that a fresh
        # DB (tables don't exist yet) and an already-initialized DB both work.
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
            except Exception:
                pass
            try:
                await conn_fr.execute("""
                    SELECT create_hypertable(
                        'funding_rates', 'time',
                        if_not_exists => TRUE,
                        migrate_data  => TRUE
                    );
                """)
            except Exception:
                pass
            try:
                await conn_fr.execute("""
                    CREATE INDEX IF NOT EXISTS idx_funding_rates_sym_ex
                        ON funding_rates (symbol, exchange, time DESC);
                """)
            except Exception:
                pass

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
                except Exception:
                    pass  # table may not exist yet (ohlcv_1m on first boot)


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
        return timedelta(minutes=1)
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
          AND bucket > NOW() - ($4 * $1::interval)
        GROUP BY time_bucket($1, bucket), exchange, symbol
        ORDER BY 1 DESC
        """,
        td, symbol, exchange, limit,
    )
    if rows:
        return rows
    # Fallback: aggregate directly from price_ticks (first minutes after cold start)
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
          AND ts > NOW() - ($4 * $1)
        GROUP BY bucket, exchange, symbol
        ORDER BY bucket DESC
        """,
        td, symbol, exchange, limit,
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


async def update_instrument(id_: int, **fields) -> asyncpg.Record | None:
    """Update arbitrary fields on an instrument row."""
    pool = await get_pool()
    if not fields:
        return await pool.fetchrow("SELECT * FROM instruments WHERE id = $1", id_)

    # Serialize aliases dict to JSON string if present
    if "aliases" in fields and isinstance(fields["aliases"], dict):
        fields["aliases"] = json.dumps(fields["aliases"])

    cols = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    vals = list(fields.values())
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
    if exchange:
        return await pool.fetch(
            """
            SELECT ts, symbol, exchange, open, high, low, close, base_volume, quote_volume
            FROM ohlcv_daily
            WHERE symbol = $1 AND exchange = $2
            ORDER BY ts ASC
            LIMIT $3
            """,
            symbol, exchange, limit,
        )
    # Aggregate across exchanges: sum volumes, OHLC from first/max/min/last exchange data
    return await pool.fetch(
        """
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
        GROUP BY ts
        ORDER BY ts ASC
        LIMIT $2
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
            WHERE ts >= '2026-01-01'
            GROUP BY ts, symbol
        ) daily
        GROUP BY symbol
        ORDER BY symbol
        """
    )


async def fetch_weekly_adtv_rub() -> list[asyncpg.Record]:
    """
    Weekly ADTV in RUB per symbol × exchange × ISO week.

    Crypto volumes are converted USDT → RUB by joining with moex_fx_rates
    (USDRUBF daily settlement price, forward-filled for weekends).
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
            INNER JOIN moex_fx_rates fx ON fx.date = o.ts::date
            WHERE o.ts >= '2026-01-01'
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
                END                                                      AS symbol,
                'moex'::text                                             AS exchange,
                COUNT(*)                                                 AS days_in_week,
                ROUND(
                    (SUM(m.value_rub) / NULLIF(COUNT(*), 0))::numeric, 2
                )                                                        AS adtv
            FROM moex_daily_value m
            WHERE m.date >= '2026-01-01'
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

    Crypto volumes: quote_volume_USDT × daily USDRUBF rate.
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
            INNER JOIN moex_fx_rates fx ON fx.date = o.ts::date
            WHERE o.ts::date >= CURRENT_DATE - INTERVAL '30 days'
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


async def fetch_history_metrics_by_exchange() -> list[asyncpg.Record]:
    """Per-exchange ADTV breakdown used for the detail tooltip."""
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
        WHERE ts >= '2026-01-01'
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
