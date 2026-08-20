"""
Hourly equity-perp volume ETL (TradFi Market Share → Hourly).

The daily ETL (``stocks/etl.py``) covers the whole equity-perp universe but only
at day granularity, and ``ohlcv_hourly`` has hourly bars for the handful of
curated instruments only — so neither can answer "which hours carry the US
market".  This loop fills that gap: the same universe, 1-hour candles, into
``stock_hourly_volume``.

Turnover per hour = close × volume × contractSize (contractSize only matters on
MEXC).  MEXC klines emit placeholder bars dated into the future — those are
dropped, as in the daily backfills.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.exchanges import make_exchange
from app.db.timescale import get_stock_hourly_latest_ts, upsert_stock_hourly_volume
from app.stocks.config import STOCK_EXCHANGES
from app.stocks.etl import build_stock_universe

log = logging.getLogger(__name__)

TIMEFRAME = "1h"
# Must not exceed the stock_hourly_volume retention window (45 days), and there
# is no point fetching deeper than the profile shows (30 days).
BACKFILL_DAYS = 30
# Re-fetch this far back every pass so the still-forming hour and any late
# exchange corrections are overwritten.
LOOKBACK_HOURS = 6
REFRESH_INTERVAL = 3600           # one pass per newly closed hour
# The universe is ~520 pairs; starting in lockstep with the daily stock ETL and
# the crypto hourly backfill is what tips Hyperliquid into 429, so let those
# finish first.
STARTUP_DELAY_SEC = 180

_THROTTLE_SEC = 0.15              # gentle pacing between per-instrument fetches
# Bitget answers with an empty page when `since` predates the contract's launch;
# step forward instead of giving up so late-listed perps still backfill.
_EMPTY_SKIP_MS = 7 * 86_400_000
# Rows are flushed to the DB in batches instead of accumulating a whole pass.
# The first pass spans ~865 pairs × 720 hours ≈ 620 000 rows; holding those in a
# dict cost ~175 MB and climbing, against a container limit of 1 GiB that already
# sits at ~740 MB idle — the pass was heading for an OOM kill of the backend.
_FLUSH_ROWS = 20_000

_running = asyncio.Lock()


def accumulate_bars(
    bars: list,
    exchange_id: str,
    ticker: str,
    contract_size: float,
    since_ms: int,
    now_ms: int,
    out: dict,
) -> None:
    """
    Fold one page of 1-hour candles into ``out`` keyed by (hour, exchange, ticker).

    Bars outside the requested window are ignored, and — like both other kline
    consumers — bars stamped in the future are dropped: MEXC returns placeholder
    candles years ahead for some symbols.
    """
    for bar in bars:
        ts, _o, _h, _l, close, vol = bar[:6]
        if ts > now_ms or ts < since_ms or not close or not vol:
            continue
        hour = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        key = (hour, exchange_id, ticker)
        out[key] = out.get(key, 0.0) + close * vol * contract_size


async def _flush(buf: dict) -> int:
    """Upsert and clear the buffer.  Rows are keyed (hour, exchange, ticker)."""
    if not buf:
        return 0
    rows = [(h, e, t, round(v, 2)) for (h, e, t), v in buf.items()]
    buf.clear()
    return await upsert_stock_hourly_volume(rows)


async def _fetch_exchange(exchange_id: str, insts: list[tuple], since_ms: int,
                          now_ms: int) -> int:
    """
    Walk one exchange's instruments sequentially, writing turnover as it goes.

    Each instrument's pagination completes before the next one starts, so a
    partial flush never splits an hour's value across two upserts.  Rows stored.
    """
    ex = make_exchange(exchange_id, "swap")
    out: dict = {}
    stored = 0
    try:
        for sym, ticker, cs in insts:
            # Paginate by advancing `since`: OKX caps hourly klines at 300 bars
            # per request regardless of `limit`, so one fetch would truncate a
            # 30-day window.  Advance until the newest bar stops moving forward.
            cur = since_ms
            prev_last = None
            while cur < now_ms:
                try:
                    bars = await ex.fetch_ohlcv(sym, TIMEFRAME, since=cur, limit=1000)
                except Exception as exc:
                    log.debug("Stock hourly ETL: %s %s fetch failed: %s", exchange_id, sym, exc)
                    break
                if not bars:
                    cur += _EMPTY_SKIP_MS
                    continue
                accumulate_bars(bars, exchange_id, ticker, cs, since_ms, now_ms, out)
                last = bars[-1][0]
                next_cur = last + 1
                if last >= now_ms or (prev_last is not None and last <= prev_last) or next_cur <= cur:
                    break
                prev_last = last
                cur = next_cur
                await asyncio.sleep(_THROTTLE_SEC)
            if len(out) >= _FLUSH_ROWS:
                stored += await _flush(out)
            await asyncio.sleep(_THROTTLE_SEC)
        stored += await _flush(out)
    finally:
        await ex.close()
    return stored


def since_ms_for(latest, now: datetime | None = None) -> int:
    """
    Start timestamp for one exchange's fetch.

    Nothing stored for that venue → the full backfill window; otherwise a short
    lookback before its newest bar, so a pass costs one page per instrument
    instead of re-downloading a month.
    """
    now = now or datetime.now(tz=timezone.utc)
    floor = now - timedelta(days=BACKFILL_DAYS)
    if latest is None:
        return int(floor.timestamp() * 1000)
    # A naive timestamp is UTC, not local — same guard as `hourly_since` in the
    # crypto backfill.
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    start = max(latest - timedelta(hours=LOOKBACK_HOURS), floor)
    return int(start.timestamp() * 1000)


async def run_stock_hourly_etl() -> None:
    """One full pass over the equity-perp universe."""
    if _running.locked():
        log.info("Stock hourly ETL: previous pass still running, skipping")
        return
    async with _running:
        started = datetime.now(tz=timezone.utc)
        try:
            # Shared cache: a second load_markets() sweep seconds after the daily
            # ETL is exactly what makes Hyperliquid answer 429.
            universe = await build_stock_universe()
        except Exception as exc:  # noqa: BLE001
            log.warning("Stock hourly ETL: universe discovery failed: %s", exc)
            return

        now_ms = int(started.timestamp() * 1000)

        async def _fetch(e: str) -> int:
            latest = await get_stock_hourly_latest_ts(e)
            return await _fetch_exchange(
                e, universe.get(e, []), since_ms_for(latest, started), now_ms
            )

        counts = await asyncio.gather(*[_fetch(e) for e in STOCK_EXCHANGES])
        took = (datetime.now(tz=timezone.utc) - started).total_seconds()
        log.info("Stock hourly ETL: pass complete — upserted %d rows in %.0fs",
                 sum(counts), took)


async def stock_hourly_etl_loop() -> None:
    """Background task: one pass per hour, after the startup rush has settled."""
    await asyncio.sleep(STARTUP_DELAY_SEC)
    while True:
        try:
            await run_stock_hourly_etl()
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.warning("Stock hourly ETL: loop iteration failed: %s", exc)
        await asyncio.sleep(REFRESH_INTERVAL)


async def run_stock_hourly_etl_safe() -> None:
    """Wrapper used by the manual refresh endpoint."""
    try:
        await run_stock_hourly_etl()
    except Exception as exc:  # noqa: BLE001
        log.warning("Stock hourly ETL: manual refresh failed: %s", exc)
