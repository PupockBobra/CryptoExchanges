"""
Funding-rate collector and historical back-fill.

Responsibilities
────────────────
* Back-fill settled funding rates from BACKFILL_SINCE for every enabled perp
  symbol, following the same pattern as ohlcv.py.
* Live poll every POLL_INTERVAL:
    - Catch newly settled rates (incremental DB update).
    - Cache current + predicted rates in Redis for the dashboard API.

Exchange support
────────────────
  binance / okx / bybit / mexc — 8-hour funding intervals
  hyperliquid                  — 1-hour funding intervals

All errors per exchange are isolated — one broken exchange never stalls others.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

import ccxt.async_support as ccxt_async

from app.config import settings
from app.db.timescale import (
    fetch_instruments,
    upsert_funding_rates,
    get_funding_rate_latest_ts,
)
from app.redis_client import get_redis

log = logging.getLogger(__name__)

BACKFILL_SINCE   = datetime(2024, 1, 1, tzinfo=timezone.utc)
POLL_INTERVAL    = 300    # seconds between live polls (5 min)
PAGE_LIMIT       = 1_000  # records per paginated request
REDIS_TTL        = 600    # 10-min Redis TTL for current rates
STARTUP_DELAY    = 120    # seconds to wait before starting backfill (let OHLCV go first)

_EXCHANGE_CLS: dict[str, type] = {
    "binance":     ccxt_async.binance,
    "okx":         ccxt_async.okx,
    "bybit":       ccxt_async.bybit,
    "mexc":        ccxt_async.mexc,
    "hyperliquid": ccxt_async.hyperliquid,
}

_PERP_MARKET_TYPE: dict[str, str] = {
    "binance":     "future",
    "okx":         "swap",
    "bybit":       "linear",
    "mexc":        "swap",
    "hyperliquid": "swap",
}

_FUNDING_INTERVAL_HOURS: dict[str, int] = {
    "binance":     8,
    "okx":         8,
    "bybit":       8,
    "mexc":        8,
    "hyperliquid": 1,   # Hyperliquid uses hourly funding
}

_backfill_lock = asyncio.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_exchange(exchange_id: str) -> ccxt_async.Exchange:
    cls = _EXCHANGE_CLS[exchange_id]
    return cls({
        "enableRateLimit": True,
        "options": {"defaultType": _PERP_MARKET_TYPE[exchange_id]},
        "timeout": 20_000,
    })


def _parse_ts(ms) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except Exception:
        return None


# ── Back-fill ─────────────────────────────────────────────────────────────────

async def _backfill_symbol(
    exchange_id: str,
    exchange_sym: str,
    canonical: str,
    since_ms: int,
) -> int:
    """Paginate through funding_rate_history and upsert to DB. Returns rows stored."""
    ih = _FUNDING_INTERVAL_HOURS.get(exchange_id, 8)
    exchange = _make_exchange(exchange_id)
    rows: list[tuple] = []
    current_since = since_ms

    try:
        # Check if we already have data — start from latest stored ts
        latest = await get_funding_rate_latest_ts(canonical, exchange_id)
        if latest is not None:
            stored_ms = int(latest.timestamp() * 1000)
            current_since = max(current_since, stored_ms + 1)

        while True:
            try:
                history = await exchange.fetch_funding_rate_history(
                    exchange_sym, since=current_since, limit=PAGE_LIMIT
                )
            except ccxt_async.NotSupported:
                log.debug(
                    "[%s] fetch_funding_rate_history not supported for %s",
                    exchange_id, canonical,
                )
                return 0
            except ccxt_async.BadSymbol:
                log.debug("[%s] symbol not found: %s", exchange_id, exchange_sym)
                return 0
            except Exception as e:
                log.warning(
                    "[%s] history fetch error for %s: %s", exchange_id, canonical, e
                )
                break

            if not history:
                break

            for item in history:
                ts   = _parse_ts(item.get("timestamp"))
                rate = item.get("fundingRate")
                if ts is None or rate is None:
                    continue
                rows.append((ts, canonical, exchange_id, float(rate), ih))

            if len(history) < PAGE_LIMIT:
                break
            last_ts = history[-1].get("timestamp")
            if last_ts is None:
                break
            current_since = int(last_ts) + 1
            await asyncio.sleep(0.3)   # polite rate-limiting between pages

    finally:
        try:
            await exchange.close()
        except Exception:
            pass

    if not rows:
        return 0

    stored = await upsert_funding_rates(rows)
    if stored:
        log.info(
            "[%s] backfilled %d funding rate records for %s",
            exchange_id, stored, canonical,
        )
    return stored


async def _run_backfill(work: list[tuple[str, str, str]]) -> None:
    """Back-fill all symbol pairs with bounded concurrency."""
    if _backfill_lock.locked():
        log.info("Funding backfill already running, skipping")
        return

    async with _backfill_lock:
        since_ms = int(BACKFILL_SINCE.timestamp() * 1000)
        sem = asyncio.Semaphore(2)   # gentle: 2 concurrent to avoid OOM on small instances

        async def limited(ex_id: str, ex_sym: str, canonical: str):
            async with sem:
                try:
                    await _backfill_symbol(ex_id, ex_sym, canonical, since_ms)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning(
                        "[%s] backfill failed for %s: %s", ex_id, canonical, e
                    )

        await asyncio.gather(*[limited(*args) for args in work], return_exceptions=True)
        log.info("Funding rate backfill complete (%d pairs)", len(work))



# ── Live poll ─────────────────────────────────────────────────────────────────

async def _poll_live(work: list[tuple[str, str, str]]) -> None:
    """
    For each (exchange_id, exchange_sym, canonical):
      1. Incremental DB update — fetch any newly settled rates.
      2. Redis update — cache current + predicted rate for the dashboard.
    """
    r = await get_redis()

    # Group by exchange to share one client instance per exchange
    by_exchange: dict[str, list[tuple[str, str]]] = {}
    for ex_id, ex_sym, canonical in work:
        by_exchange.setdefault(ex_id, []).append((ex_sym, canonical))

    for ex_id, sym_pairs in by_exchange.items():
        ih = _FUNDING_INTERVAL_HOURS.get(ex_id, 8)
        exchange = _make_exchange(ex_id)
        try:
            for ex_sym, canonical in sym_pairs:
                # 1 ── Incremental settled-rate update ────────────────────────
                latest = await get_funding_rate_latest_ts(canonical, ex_id)
                if latest:
                    since_ms = int(latest.timestamp() * 1000) + 1
                else:
                    since_ms = int(
                        (datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp() * 1000
                    )
                try:
                    new_settled = await exchange.fetch_funding_rate_history(
                        ex_sym, since=since_ms, limit=100
                    )
                    if new_settled:
                        rows = [
                            (_parse_ts(item["timestamp"]), canonical, ex_id,
                             float(item["fundingRate"]), ih)
                            for item in new_settled
                            if item.get("timestamp") and item.get("fundingRate") is not None
                        ]
                        valid_rows = [row for row in rows if row[0] is not None]
                        if valid_rows:
                            stored = await upsert_funding_rates(valid_rows)
                            if stored:
                                log.info(
                                    "[%s] live: stored %d new settled rates for %s",
                                    ex_id, stored, canonical,
                                )
                except Exception:
                    pass  # incremental update failed; try again next poll

                # 2 ── Current/predicted rate → Redis ─────────────────────────
                try:
                    fr = await exchange.fetch_funding_rate(ex_sym)
                    if fr:
                        # Use `is not None` — rate can legitimately be 0.0
                        rate = fr.get("fundingRate")
                        if rate is None:
                            rate = fr.get("previousFundingRate")
                        if rate is not None:
                            payload = {
                                "symbol":            canonical,
                                "exchange":          ex_id,
                                "rate":              float(rate),
                                "predicted_rate":    (
                                    float(fr["nextFundingRate"])
                                    if fr.get("nextFundingRate") is not None
                                    else None
                                ),
                                "next_funding_time": fr.get("nextFundingDatetime"),
                                "settlement_time":   (
                                    fr.get("fundingDatetime")
                                    or fr.get("previousFundingDatetime")
                                ),
                                "interval_hours":    ih,
                                "updated_at":        datetime.now(tz=timezone.utc).isoformat(),
                            }
                            redis_key = f"funding:current:{ex_id}:{canonical}"
                            await r.setex(redis_key, REDIS_TTL, json.dumps(payload))
                except Exception as e:
                    log.warning(
                        "[%s] fetch_funding_rate failed for %s: %s", ex_id, canonical, e
                    )

                await asyncio.sleep(0.2)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[%s] live poll error: %s", ex_id, e)
        finally:
            try:
                await exchange.close()
            except Exception:
                pass


# ── Work list builder ─────────────────────────────────────────────────────────

async def _build_work_list() -> list[tuple[str, str, str]]:
    """
    Returns [(exchange_id, exchange_symbol, canonical), ...]
    for every enabled perp instrument × supported exchange.
    """
    instruments = await fetch_instruments(enabled_only=True)
    work: list[tuple[str, str, str]] = []

    for inst in instruments:
        canonical: str = inst["canonical"]
        if ":" not in canonical:   # skip spot instruments
            continue
        raw = inst["aliases"]
        aliases: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})

        for ex_id in settings.exchanges:
            if ex_id not in _EXCHANGE_CLS:
                continue
            alias_val = aliases.get(ex_id)
            if alias_val is None and ex_id in aliases:
                continue   # explicit null → not listed on this exchange
            ex_sym = alias_val or canonical
            work.append((ex_id, ex_sym, canonical))

    return work


# ── Entry point ───────────────────────────────────────────────────────────────

async def funding_collector_loop() -> None:
    """
    Background task started by app.main on startup.
    1. Builds the work list from enabled perp instruments.
    2. Back-fills history from BACKFILL_SINCE.
    3. Live-polls every POLL_INTERVAL seconds.
    """
    log.info("Funding rate collector starting — waiting %ds for OHLCV backfill to settle…",
             STARTUP_DELAY)
    await asyncio.sleep(STARTUP_DELAY)

    work = await _build_work_list()
    if not work:
        log.warning("Funding collector: no enabled perp instruments found — exiting")
        return

    log.info(
        "Funding collector: %d exchange×symbol pairs to track", len(work)
    )

    # Phase 1 — historical backfill
    await _run_backfill(work)

    # Phase 2 — live poll loop
    while True:
        try:
            await _poll_live(work)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Funding live-poll error: %s", e, exc_info=True)
        await asyncio.sleep(POLL_INTERVAL)
