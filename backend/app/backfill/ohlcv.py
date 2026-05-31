"""
Daily OHLCV historical backfill.

Fetches 1-day candles from all configured exchanges for every enabled
instrument, starting from BACKFILL_SINCE (2026-01-01).  Runs incrementally:
only requests candles newer than what is already stored.

Usage
-----
Called automatically on backend startup and re-runs every REFRESH_INTERVAL
seconds to pick up today's (possibly incomplete) candle.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

import ccxt.async_support as ccxt_async

from app.config import settings
from app.db.timescale import (
    fetch_instruments,
    upsert_ohlcv_daily,
    get_ohlcv_daily_latest_ts,
)
from app.exchanges import EXCHANGE_CLS, PERP_MARKET_TYPE

log = logging.getLogger(__name__)

BACKFILL_SINCE   = datetime(2026, 1, 1, tzinfo=timezone.utc)
TIMEFRAME        = "1d"
REFRESH_INTERVAL = 6 * 3600   # re-run every 6 hours to refresh today's partial candle

# Global lock prevents multiple concurrent backfill runs from fighting over DB locks
_backfill_running = asyncio.Lock()


def _make_exchange(exchange_id: str, is_perp: bool) -> ccxt_async.Exchange:
    cls = EXCHANGE_CLS[exchange_id]
    market_type = PERP_MARKET_TYPE[exchange_id] if is_perp else "spot"
    return cls({
        "enableRateLimit": True,
        "options": {"defaultType": market_type},
        "timeout": 20000,   # 20 s per request; default 10 s is too short for Bybit/OKX
    })


async def _backfill_one(
    exchange_id: str,
    exchange_sym: str,
    canonical: str,
    has_alias: bool = False,
) -> int:
    """
    Fetch daily OHLCV for one exchange × symbol pair and upsert into DB.
    Returns the number of rows stored.

    has_alias=True means the caller resolved a specific exchange alias for this
    symbol, so we skip the load_markets() existence check and attempt
    fetch_ohlcv directly (avoids OKX/Bybit load_markets timeouts on option
    instrument fetches).
    """
    is_perp = ":" in canonical
    exchange = _make_exchange(exchange_id, is_perp)
    stored = 0
    try:
        # MEXC swap markets always need load_markets() to retrieve contractSize.
        # Other exchanges with an explicit alias skip it (avoids OKX/Bybit option timeouts).
        need_markets = (not has_alias) or (exchange_id == "mexc" and is_perp)
        if need_markets:
            await exchange.load_markets()
            if not has_alias and exchange_sym not in exchange.markets:
                log.debug("[%s] %s not in markets, skipping", exchange_id, exchange_sym)
                return 0

        # MEXC contract kline API returns `vol` in raw contract units, not base currency.
        # Multiply by contractSize to get the true base-asset volume.
        # Spot markets (BTC/USDT, ETH/USDT …) are fetched from the spot endpoint and
        # already report volume in base currency — no adjustment needed.
        contract_size = 1.0
        if exchange_id == "mexc" and is_perp:
            mkt = exchange.markets.get(exchange_sym) if exchange.markets else None
            if mkt:
                contract_size = float(mkt.get("contractSize") or 1)

        # Determine start timestamp
        latest = await get_ohlcv_daily_latest_ts(canonical, exchange_id)
        if latest is None:
            since_dt = BACKFILL_SINCE
        else:
            # Re-fetch from 2 days before latest so today's partial candle gets updated
            since_dt = latest.replace(tzinfo=timezone.utc) - timedelta(days=2)
            since_dt = max(since_dt, BACKFILL_SINCE)

        since_ms = int(since_dt.timestamp() * 1000)
        rows_to_upsert: list[tuple] = []

        while True:
            try:
                batch = await exchange.fetch_ohlcv(
                    exchange_sym, TIMEFRAME, since=since_ms, limit=500
                )
            except Exception as exc:
                log.warning("[%s] fetch_ohlcv %s: %s", exchange_id, exchange_sym, exc)
                break

            if not batch:
                break

            for bar in batch:
                ts_ms, open_, high, low, close, volume = bar[:6]
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                # Apply contract size normalisation for MEXC perps
                base_vol  = volume * contract_size
                quote_vol = round(close * base_vol, 4) if close and base_vol else 0.0
                rows_to_upsert.append(
                    (ts, canonical, exchange_id, open_, high, low, close, base_vol, quote_vol)
                )

            last_ts_ms = batch[-1][0]
            if len(batch) < 500:
                break
            since_ms = last_ts_ms + 1
            await asyncio.sleep(0.3)   # polite rate limiting between pages

        stored = await upsert_ohlcv_daily(rows_to_upsert)
        if stored:
            log.info("[%s] upserted %d daily bars for %s", exchange_id, stored, canonical)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("[%s] backfill error for %s: %s", exchange_id, canonical, exc, exc_info=True)
    finally:
        try:
            await exchange.close()
        except Exception:
            pass

    return stored


async def run_backfill() -> None:
    """
    Run a full incremental backfill for all enabled instruments × exchanges.
    Safe to call multiple times — it only fetches what is missing.
    A module-level lock ensures only one run executes at a time.
    """
    if _backfill_running.locked():
        log.info("OHLCV backfill already running, skipping this request")
        return
    async with _backfill_running:
        await _run_backfill_inner()


async def _run_backfill_inner() -> None:
    log.info("OHLCV backfill started (since %s)", BACKFILL_SINCE.date())

    instruments = await fetch_instruments(enabled_only=True)
    if not instruments:
        log.info("No enabled instruments found, skipping backfill")
        return

    # Build work list: [(exchange_id, exchange_symbol, canonical, has_alias), ...]
    work: list[tuple[str, str, str, bool]] = []
    for inst in instruments:
        canonical: str = inst["canonical"]
        # asyncpg returns JSONB columns as raw strings — parse them
        raw = inst["aliases"]
        aliases: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})
        for ex_id in settings.exchanges:
            # Resolve the symbol name this exchange uses
            alias_val = aliases.get(ex_id)
            if alias_val is None and ex_id in aliases:
                # Explicit null → this exchange doesn't list the instrument
                continue
            has_alias  = alias_val is not None   # True when a specific alias was configured
            exchange_sym = alias_val or canonical
            work.append((ex_id, exchange_sym, canonical, has_alias))

    # Run with bounded concurrency (4 at a time) to respect rate limits
    sem = asyncio.Semaphore(4)

    async def limited(ex_id: str, ex_sym: str, can: str, alias: bool):
        async with sem:
            await _backfill_one(ex_id, ex_sym, can, has_alias=alias)

    await asyncio.gather(*[limited(*args) for args in work], return_exceptions=True)
    log.info("OHLCV backfill complete (%d exchange×symbol pairs processed)", len(work))


async def backfill_loop() -> None:
    """
    Background task: run backfill once on startup, then refresh every
    REFRESH_INTERVAL seconds to pick up newly completed daily candles.
    """
    while True:
        try:
            await run_backfill()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Backfill loop error: %s", exc, exc_info=True)
        await asyncio.sleep(REFRESH_INTERVAL)
