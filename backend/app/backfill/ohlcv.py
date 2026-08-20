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

from app.config import settings
from app.db.timescale import (
    fetch_instruments,
    upsert_ohlcv_daily,
    get_ohlcv_daily_latest_ts,
)
from app.exchanges import make_exchange, CRYPTO_PERP_OVERRIDES

log = logging.getLogger(__name__)

BACKFILL_SINCE   = datetime(2026, 1, 1, tzinfo=timezone.utc)
TIMEFRAME        = "1d"
REFRESH_INTERVAL = 6 * 3600   # re-run every 6 hours to refresh today's partial candle

# BTC/ETH/SOL trading volume is sourced from PERPETUAL futures, not spot, and
# stored under the same canonical symbol — so every volume chart (Analytics /
# Daily Volume / History) shows perp volume while Realtime Prices is unaffected.
# Mapping lives in app.exchanges (shared with the OI collector).
PERP_VOLUME_OVERRIDES = CRYPTO_PERP_OVERRIDES

# Global lock prevents multiple concurrent backfill runs from fighting over DB locks
_backfill_running = asyncio.Lock()


def parse_ohlcv_batch(
    batch: list,
    now_ms: int,
    canonical: str,
    exchange_id: str,
    contract_size: float = 1.0,
) -> tuple[list[tuple], bool]:
    """
    Convert one fetch_ohlcv page into ohlcv_daily upsert rows.

    Drops bars dated after now_ms — some exchanges (notably MEXC) return
    placeholder daily candles years into the future — and reports whether any
    were seen, so the pagination loop stops instead of walking decades forward
    and writing tens of thousands of bogus rows.

    contract_size ≠ 1 only for MEXC perps, whose kline `vol` is in raw contract
    units rather than base currency.
    """
    rows: list[tuple] = []
    saw_future = False
    for bar in batch:
        ts_ms, open_, high, low, close, volume = bar[:6]
        if ts_ms > now_ms:
            saw_future = True
            continue
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        base_vol  = volume * contract_size
        quote_vol = round(close * base_vol, 4) if close and base_vol else 0.0
        rows.append(
            (ts, canonical, exchange_id, open_, high, low, close, base_vol, quote_vol)
        )
    return rows, saw_future


async def _backfill_one(
    exchange_id: str,
    exchange_sym: str,
    canonical: str,
    has_alias: bool = False,
    force_perp: bool = False,
) -> int:
    """
    Fetch daily OHLCV for one exchange × symbol pair and upsert into DB.
    Returns the number of rows stored.

    has_alias=True means the caller resolved a specific exchange alias for this
    symbol, so we skip the load_markets() existence check and attempt
    fetch_ohlcv directly (avoids OKX/Bybit load_markets timeouts on option
    instrument fetches).

    force_perp=True fetches from the perpetual market even though the canonical
    symbol has no ":" (used for BTC/ETH/SOL — see PERP_VOLUME_OVERRIDES).
    """
    is_perp = (":" in canonical) or force_perp
    market_type = None if is_perp else "spot"
    exchange = make_exchange(exchange_id, market_type=market_type)
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

        # Determine start timestamp.
        # Perp-override symbols (BTC/ETH/SOL) always re-fetch the FULL history
        # from BACKFILL_SINCE: a database may still hold the old SPOT rows under
        # the same canonical, and the incremental "latest − 2d" path would leave
        # that spot history in place while switching only the last few days to
        # perp. Re-fetching the full range overwrites every bar with perp data
        # (self-heals existing DBs without a manual DELETE).
        latest = None if force_perp else await get_ohlcv_daily_latest_ts(canonical, exchange_id)
        if latest is None:
            since_dt = BACKFILL_SINCE
        else:
            # Re-fetch from 2 days before latest so today's partial candle gets updated
            since_dt = latest.replace(tzinfo=timezone.utc) - timedelta(days=2)
            since_dt = max(since_dt, BACKFILL_SINCE)

        since_ms = int(since_dt.timestamp() * 1000)
        # Drop bars dated after this cutoff. Some exchanges (notably MEXC)
        # return placeholder daily candles for years into the future; without
        # this guard the pagination loop walks decades forward and writes
        # tens of thousands of bogus rows.
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
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

            page_rows, saw_future = parse_ohlcv_batch(
                batch, now_ms, canonical, exchange_id, contract_size
            )
            rows_to_upsert.extend(page_rows)

            last_ts_ms = batch[-1][0]
            # Stop once the page contains future bars or runs short.
            if saw_future or len(batch) < 500 or last_ts_ms >= now_ms:
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

    # Build work list: [(exchange_id, exchange_symbol, canonical, has_alias, force_perp), ...]
    work: list[tuple[str, str, str, bool, bool]] = []
    for inst in instruments:
        canonical: str = inst["canonical"]
        # asyncpg returns JSONB columns as raw strings — parse them
        raw = inst["aliases"]
        aliases: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})
        # Crypto majors source their volume from perpetual futures, not spot.
        perp_override = PERP_VOLUME_OVERRIDES.get(canonical)
        for ex_id in settings.exchanges:
            if perp_override is not None:
                ex_sym = perp_override.get(ex_id)
                if ex_sym is None:
                    continue   # exchange has no perp for this base
                work.append((ex_id, ex_sym, canonical, True, True))
                continue
            # Resolve the symbol name this exchange uses
            alias_val = aliases.get(ex_id)
            if alias_val is None and ex_id in aliases:
                # Explicit null → this exchange doesn't list the instrument
                continue
            has_alias  = alias_val is not None   # True when a specific alias was configured
            exchange_sym = alias_val or canonical
            work.append((ex_id, exchange_sym, canonical, has_alias, False))

    # Run with bounded concurrency (4 at a time) to respect rate limits
    sem = asyncio.Semaphore(4)

    async def limited(ex_id: str, ex_sym: str, can: str, alias: bool, perp: bool):
        async with sem:
            await _backfill_one(ex_id, ex_sym, can, has_alias=alias, force_perp=perp)

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
