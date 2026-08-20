"""
Top-N crypto-perp volume ETL (daily + hourly).

Feeds the Cryptocurrencies slice of the asset-group charts on TradFi Market
Share, which used to be just the three curated majors while the other slices
covered their whole universe.

Both passes share one walker: per exchange the instruments are fetched
sequentially and written in batches, so a pass never holds a whole backfill in
memory (the hourly one spans ~600 pairs × 720 hours).

Turnover = close × volume × contractSize (contractSize matters on MEXC only).
Bars stamped into the future are dropped — MEXC returns placeholder candles
years ahead for some symbols.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from app.exchanges import make_exchange
from app.db.timescale import (
    get_crypto_top_daily_latest,
    get_crypto_top_hourly_latest,
    upsert_crypto_top_daily,
    upsert_crypto_top_hourly,
)
from app.crypto.config import (
    BACKFILL_SINCE, CRYPTO_EXCHANGES, TOP_N, rank_top,
)

log = logging.getLogger(__name__)

HOURLY_BACKFILL_DAYS = 30         # must stay inside the table's 45-day retention
HOURLY_LOOKBACK_HOURS = 6
DAILY_LOOKBACK_DAYS = 2
DAILY_REFRESH_INTERVAL = 21_600   # 6 h, same cadence as the stock ETL
HOURLY_REFRESH_INTERVAL = 3600
# Both loops start after the boot rush: a `load_markets()` sweep in the middle of
# it is what makes Hyperliquid answer 429 (it costs the pass a whole cycle).
DAILY_STARTUP_DELAY_SEC = 240
HOURLY_STARTUP_DELAY_SEC = 300

_THROTTLE_SEC = 0.15
_EMPTY_SKIP_MS = 30 * 86_400_000  # Bitget answers empty before a contract's launch
_FLUSH_ROWS = 20_000              # see the OOM note in stocks/hourly_etl.py

_daily_running = asyncio.Lock()
_hourly_running = asyncio.Lock()

_UNIVERSE_TTL = timedelta(hours=6)
_last_universe: dict[str, list[tuple]] = {}
_last_universe_at: datetime | None = None


async def _discover(exchange_id: str) -> list[tuple]:
    """Top-N crypto perps on one exchange, ranked by 24h turnover."""
    ex = make_exchange(exchange_id, "swap")
    try:
        markets = await ex.load_markets()
        tickers = await ex.fetch_tickers()
    finally:
        await ex.close()
    return rank_top(markets, tickers, exchange_id, TOP_N)


async def build_crypto_universe(max_age: timedelta = _UNIVERSE_TTL) -> dict[str, list[tuple]]:
    """
    {exchange: [(symbol, base, contract_size), …]} — the ranking, cached.

    Re-ranking on every pass is the point (the top-100 churns), but the daily and
    hourly loops must not each pay for their own sweep of six exchanges.
    """
    global _last_universe, _last_universe_at

    now = datetime.now(tz=timezone.utc)
    if _last_universe and _last_universe_at and now - _last_universe_at <= max_age:
        return _last_universe

    universe: dict[str, list[tuple]] = {}
    for e in CRYPTO_EXCHANGES:
        try:
            universe[e] = await _discover(e)
            log.info("Crypto top ETL: %s → %d perps in the top-%d", e, len(universe[e]), TOP_N)
        except Exception as exc:  # noqa: BLE001 — one bad venue must not sink the rest
            log.warning("Crypto top ETL: %s discovery failed: %s", e, exc)
            universe[e] = _last_universe.get(e, [])

    if any(universe.values()):
        _last_universe, _last_universe_at = universe, now
    return universe


async def _flush(buf: dict, upsert) -> int:
    if not buf:
        return 0
    rows = [(b, e, s, round(v, 2)) for (b, e, s), v in buf.items()]
    buf.clear()
    return await upsert(rows)


async def _walk_exchange(exchange_id: str, insts: list[tuple], timeframe: str,
                         since_ms: int, now_ms: int, bucket_of, upsert) -> int:
    """Fetch every instrument of one exchange, writing turnover in batches."""
    ex = make_exchange(exchange_id, "swap")
    out: dict = {}
    stored = 0
    try:
        for sym, base, cs in insts:
            # Advance `since` until the newest bar stops moving: OKX caps hourly
            # klines at 300 per request and Bitget daily ones at ~90, so a single
            # fetch would silently truncate the window.
            cur, prev_last = since_ms, None
            while cur < now_ms:
                try:
                    bars = await ex.fetch_ohlcv(sym, timeframe, since=cur, limit=1000)
                except Exception as exc:
                    log.debug("Crypto top ETL: %s %s fetch failed: %s", exchange_id, sym, exc)
                    break
                if not bars:
                    cur += _EMPTY_SKIP_MS
                    continue
                for bar in bars:
                    ts, _o, _h, _l, close, vol = bar[:6]
                    if ts > now_ms or ts < since_ms or not close or not vol:
                        continue
                    key = (bucket_of(ts), exchange_id, base)
                    out[key] = out.get(key, 0.0) + close * vol * cs
                last = bars[-1][0]
                next_cur = last + 1
                if last >= now_ms or (prev_last is not None and last <= prev_last) or next_cur <= cur:
                    break
                prev_last = last
                cur = next_cur
                await asyncio.sleep(_THROTTLE_SEC)
            if len(out) >= _FLUSH_ROWS:
                stored += await _flush(out, upsert)
            await asyncio.sleep(_THROTTLE_SEC)
        stored += await _flush(out, upsert)
    finally:
        await ex.close()
    return stored


def _day_of(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()


def _hour_of(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def daily_since_ms(latest, now: datetime | None = None) -> int:
    """Empty venue → the YTD floor; otherwise a couple of days before its newest row."""
    now = now or datetime.now(tz=timezone.utc)
    floor = datetime.combine(date.fromisoformat(BACKFILL_SINCE), datetime.min.time(),
                             tzinfo=timezone.utc)
    if latest is None:
        return int(floor.timestamp() * 1000)
    start = datetime.combine(latest, datetime.min.time(), tzinfo=timezone.utc) \
        - timedelta(days=DAILY_LOOKBACK_DAYS)
    return int(max(start, floor).timestamp() * 1000)


def hourly_since_ms(latest, now: datetime | None = None) -> int:
    """Empty venue → the retention window; otherwise a short lookback."""
    now = now or datetime.now(tz=timezone.utc)
    floor = now - timedelta(days=HOURLY_BACKFILL_DAYS)
    if latest is None:
        return int(floor.timestamp() * 1000)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return int(max(latest - timedelta(hours=HOURLY_LOOKBACK_HOURS), floor).timestamp() * 1000)


async def _run(label: str, lock: asyncio.Lock, timeframe: str, bucket_of,
               latest_of, since_of, upsert) -> None:
    if lock.locked():
        log.info("Crypto top ETL (%s): previous pass still running, skipping", label)
        return
    async with lock:
        started = datetime.now(tz=timezone.utc)
        universe = await build_crypto_universe()
        if not any(universe.values()):
            log.warning("Crypto top ETL (%s): empty universe, skipping pass", label)
            return
        now_ms = int(started.timestamp() * 1000)

        async def _one(e: str) -> int:
            return await _walk_exchange(
                e, universe.get(e, []), timeframe,
                since_of(await latest_of(e), started), now_ms, bucket_of, upsert,
            )

        counts = await asyncio.gather(*[_one(e) for e in CRYPTO_EXCHANGES])
        took = (datetime.now(tz=timezone.utc) - started).total_seconds()
        log.info("Crypto top ETL (%s): pass complete — upserted %d rows in %.0fs",
                 label, sum(counts), took)


async def run_crypto_daily_etl() -> None:
    await _run("daily", _daily_running, "1d", _day_of,
               get_crypto_top_daily_latest, daily_since_ms, upsert_crypto_top_daily)


async def run_crypto_hourly_etl() -> None:
    await _run("hourly", _hourly_running, "1h", _hour_of,
               get_crypto_top_hourly_latest, hourly_since_ms, upsert_crypto_top_hourly)


async def crypto_daily_etl_loop() -> None:
    await asyncio.sleep(DAILY_STARTUP_DELAY_SEC)
    while True:
        try:
            await run_crypto_daily_etl()
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.warning("Crypto top ETL (daily): loop iteration failed: %s", exc)
        await asyncio.sleep(DAILY_REFRESH_INTERVAL)


async def crypto_hourly_etl_loop() -> None:
    await asyncio.sleep(HOURLY_STARTUP_DELAY_SEC)
    while True:
        try:
            await run_crypto_hourly_etl()
        except Exception as exc:  # noqa: BLE001
            log.warning("Crypto top ETL (hourly): loop iteration failed: %s", exc)
        await asyncio.sleep(HOURLY_REFRESH_INTERVAL)


async def run_crypto_etl_safe() -> None:
    """Both passes, for the manual refresh endpoint."""
    for run in (run_crypto_daily_etl, run_crypto_hourly_etl):
        try:
            await run()
        except Exception as exc:  # noqa: BLE001
            log.warning("Crypto top ETL: manual refresh failed: %s", exc)
