"""
Hourly OHLCV backfill for crypto exchanges (Hourly Volume page).

Same idea as ``backfill/ohlcv.py`` but with a 1-hour timeframe and a rolling
window instead of a fixed start date: hourly bars are only kept for
``HOURLY_RETENTION_DAYS`` (see the retention policy on ``ohlcv_hourly``), so
fetching further back would just feed rows to the retention job.

Only crypto exchanges are covered — MOEX publishes no rouble turnover in its
intraday candles and SPB/stocks have their own daily ETLs.

The bar parser is shared with the daily backfill (``parse_ohlcv_batch``), which
already drops MEXC's placeholder bars dated into the future.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

from app.backfill.ohlcv import parse_ohlcv_batch
from app.config import settings
from app.db.timescale import (
    fetch_instruments,
    upsert_ohlcv_hourly,
    get_ohlcv_hourly_latest_ts,
)
from app.exchanges import make_exchange, CRYPTO_PERP_OVERRIDES

log = logging.getLogger(__name__)

TIMEFRAME = "1h"
# Must not exceed the ohlcv_hourly retention window (90 days) — anything older
# is deleted by the retention job right after we write it.
HOURLY_BACKFILL_DAYS = 90
# Re-fetch this far back on every pass so the most recent (still forming) bars
# and any late exchange corrections are overwritten.
HOURLY_LOOKBACK_HOURS = 6
REFRESH_INTERVAL = 3600   # hourly — one pass per newly closed bar

_backfill_running = asyncio.Lock()


def hourly_since(latest_ts, now: datetime | None = None) -> datetime:
    """
    Start timestamp for one pair's fetch.

    Empty table → the full retention window.  Otherwise a short lookback before
    the newest stored bar, so the in-progress hour is refreshed without
    re-downloading months of history every pass.
    """
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=HOURLY_BACKFILL_DAYS)
    if latest_ts is None:
        return floor
    since = latest_ts.replace(tzinfo=timezone.utc) - timedelta(hours=HOURLY_LOOKBACK_HOURS)
    return max(since, floor)


async def _fetch_pair(
    exchange,
    exchange_id: str,
    exchange_sym: str,
    canonical: str,
    contract_size: float,
) -> int:
    """Fetch hourly bars for one symbol on an already-open exchange.  Rows stored."""
    latest   = await get_ohlcv_hourly_latest_ts(canonical, exchange_id)
    since_ms = int(hourly_since(latest).timestamp() * 1000)
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)

    rows_to_upsert: list[tuple] = []
    while since_ms < now_ms:
        try:
            batch = await exchange.fetch_ohlcv(
                exchange_sym, TIMEFRAME, since=since_ms, limit=1000
            )
        except Exception as exc:
            log.warning("[%s] fetch_ohlcv 1h %s: %s", exchange_id, exchange_sym, exc)
            break

        if not batch:
            break

        page_rows, saw_future = parse_ohlcv_batch(
            batch, now_ms, canonical, exchange_id, contract_size
        )
        rows_to_upsert.extend(page_rows)

        # A short page is normal — OKX caps hourly klines at 300 bars per request
        # regardless of `limit` — so advance until the newest bar stops moving
        # forward rather than stopping on page length.
        last_ts_ms = batch[-1][0]
        next_since = last_ts_ms + 1
        if saw_future or last_ts_ms >= now_ms or next_since <= since_ms:
            break
        since_ms = next_since
        await asyncio.sleep(0.3)

    stored = await upsert_ohlcv_hourly(rows_to_upsert)
    if stored:
        log.debug("[%s] upserted %d hourly bars for %s", exchange_id, stored, canonical)
    return stored


async def _backfill_exchange(exchange_id: str, jobs: list[tuple[str, str, bool]]) -> int:
    """
    Fetch every tracked symbol on one exchange through a single ccxt instance.

    One instance per exchange rather than one per symbol: `load_markets()` is the
    dominant cost of a pass (and the call Hyperliquid rate-limits), so calling it
    once per exchange instead of ~30 times keeps an hourly schedule affordable.

    `jobs` are (exchange_symbol, canonical, is_perp) — perp and spot markets need
    separate ccxt instances, so the caller groups them and calls this twice.
    """
    if not jobs:
        return 0
    is_perp  = jobs[0][2]
    exchange = make_exchange(exchange_id, market_type=None if is_perp else "spot")
    stored = 0
    try:
        try:
            await exchange.load_markets()
        except Exception as exc:
            log.warning("[%s] load_markets failed, skipping this pass: %s", exchange_id, exc)
            return 0

        for exchange_sym, canonical, _ in jobs:
            if exchange_sym not in exchange.markets:
                log.debug("[%s] %s not in markets, skipping", exchange_id, exchange_sym)
                continue
            # MEXC contract klines report `vol` in contract units, not base currency.
            contract_size = 1.0
            if exchange_id == "mexc" and is_perp:
                mkt = exchange.markets.get(exchange_sym)
                if mkt:
                    contract_size = float(mkt.get("contractSize") or 1)
            try:
                stored += await _fetch_pair(
                    exchange, exchange_id, exchange_sym, canonical, contract_size
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[%s] hourly backfill error for %s: %s", exchange_id, canonical, exc)

        log.info("[%s] hourly pass done — %d bars over %d symbols", exchange_id, stored, len(jobs))
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await exchange.close()
        except Exception:
            pass

    return stored


def build_work_list(instruments: list) -> list[tuple[str, str, str, bool, bool]]:
    """
    Expand instruments × exchanges into fetch jobs.

    Mirrors the daily backfill: crypto majors are read from their perp contract
    (CRYPTO_PERP_OVERRIDES) and an explicit ``null`` alias means the exchange
    does not list the instrument.
    """
    work: list[tuple[str, str, str, bool, bool]] = []
    for inst in instruments:
        canonical: str = inst["canonical"]
        raw = inst["aliases"]
        aliases: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})
        perp_override = CRYPTO_PERP_OVERRIDES.get(canonical)
        for ex_id in settings.exchanges:
            if perp_override is not None:
                ex_sym = perp_override.get(ex_id)
                if ex_sym is None:
                    continue
                work.append((ex_id, ex_sym, canonical, True, True))
                continue
            alias_val = aliases.get(ex_id)
            if alias_val is None and ex_id in aliases:
                continue
            work.append((ex_id, alias_val or canonical, canonical, alias_val is not None, False))
    return work


def group_by_exchange(
    work: list[tuple[str, str, str, bool, bool]],
) -> dict[tuple[str, bool], list[tuple[str, str, bool]]]:
    """
    Bucket the work list by (exchange, is_perp) — one ccxt instance per bucket.

    Spot and perp markets need different ccxt instances, so they cannot share a
    bucket even on the same exchange.
    """
    buckets: dict[tuple[str, bool], list[tuple[str, str, bool]]] = {}
    for ex_id, ex_sym, canonical, _has_alias, force_perp in work:
        is_perp = (":" in canonical) or force_perp
        buckets.setdefault((ex_id, is_perp), []).append((ex_sym, canonical, is_perp))
    return buckets


async def run_hourly_backfill() -> None:
    """One incremental pass over all enabled instruments × crypto exchanges."""
    if _backfill_running.locked():
        log.info("Hourly backfill already running, skipping this request")
        return
    async with _backfill_running:
        instruments = await fetch_instruments(enabled_only=True)
        if not instruments:
            log.info("Hourly backfill: no enabled instruments")
            return

        work    = build_work_list(instruments)
        buckets = group_by_exchange(work)
        log.info("Hourly backfill started (%d pairs over %d exchange buckets, %d-day window)",
                 len(work), len(buckets), HOURLY_BACKFILL_DAYS)

        # Exchanges run in parallel; symbols within one exchange run sequentially
        # on its shared instance, which is what keeps the request rate polite.
        await asyncio.gather(
            *[_backfill_exchange(ex_id, jobs) for (ex_id, _perp), jobs in buckets.items()],
            return_exceptions=True,
        )
        log.info("Hourly backfill complete (%d pairs)", len(work))


async def hourly_backfill_loop() -> None:
    """Background task: run at startup, then once per hour."""
    while True:
        try:
            await run_hourly_backfill()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Hourly backfill loop error: %s", exc, exc_info=True)
        await asyncio.sleep(REFRESH_INTERVAL)
