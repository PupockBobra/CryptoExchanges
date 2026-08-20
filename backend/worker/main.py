"""Collector worker — run as a separate process alongside the FastAPI backend."""
import asyncio
import json
import logging

from app.config import settings
from app.db.timescale import init_db, seed_instruments_from_config, fetch_instruments
from app.redis_client import get_redis, close_redis
from app.collector.binance      import BinanceCollector
from app.collector.okx         import OkxCollector
from app.collector.bybit       import BybitCollector
from app.collector.mexc        import MexcCollector
from app.collector.hyperliquid import HyperliquidCollector

log = logging.getLogger(__name__)

COLLECTORS = {
    "binance":     BinanceCollector,
    "okx":         OkxCollector,
    "bybit":       BybitCollector,
    "mexc":        MexcCollector,
    "hyperliquid": HyperliquidCollector,
}


async def load_enabled_symbols() -> tuple[list[str], dict[str, dict[str, str]]]:
    """
    Load enabled canonical symbols and their per-exchange aliases from the DB.
    Returns (canonicals, db_aliases) where db_aliases is:
        { canonical: { exchange_id: exchange_symbol, ... }, ... }
    Null alias values (meaning "this exchange doesn't list the instrument") are
    omitted so the collector falls back to the markets-availability check.
    Falls back to config symbols with empty aliases on an empty instruments table.
    """
    rows = await fetch_instruments(enabled_only=True)
    if rows:
        canonicals = [r["canonical"] for r in rows]
        db_aliases: dict[str, dict[str, str]] = {}
        for r in rows:
            raw = r["aliases"]
            aliases: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})
            # Preserve null values — _resolve_symbols uses them to skip the symbol
            # on that specific exchange without falling through to the canonical name
            db_aliases[r["canonical"]] = dict(aliases)
        return canonicals, db_aliases
    log.warning("No instruments in DB — falling back to ARBI_SYMBOLS env-var")
    return settings.symbols, {}


async def _wait_for_reload() -> None:
    """Block until an instruments:reload message arrives on Redis."""
    r = await get_redis()
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe("instruments:reload")
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                return
    finally:
        # Always release the connection — runs on normal return AND on the
        # task.cancel() that _run_collectors issues, so reloads don't leak.
        await pubsub.aclose()


async def _run_collectors(
    symbols: list[str],
    db_aliases: dict[str, dict[str, str]],
) -> None:
    """
    Start one collector task per configured exchange plus a reload-listener task.
    Returns when the reload signal fires (normal case) or a task raises unexpectedly.
    On return, the caller should reload symbols and call again.
    """
    log.info(
        "Starting collectors for %d symbols (%d spot, %d perp): %s",
        len(symbols),
        sum(1 for s in symbols if ":" not in s),
        sum(1 for s in symbols if ":" in s),
        symbols,
    )

    tasks: list[asyncio.Task] = []
    for exchange_id in settings.exchanges:
        cls = COLLECTORS.get(exchange_id)
        if cls is None:
            log.warning("No collector registered for exchange: %s", exchange_id)
            continue
        collector = cls(symbols=symbols, db_aliases=db_aliases)
        t = asyncio.create_task(collector.run(), name=f"collector-{exchange_id}")
        tasks.append(t)
        log.info("Started collector: %s", exchange_id)

    reload_task = asyncio.create_task(_wait_for_reload(), name="reload-listener")
    tasks.append(reload_task)

    # Wait for the first task to finish — normally the reload listener.
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for t in done:
        name = t.get_name()
        if t.get_name() == "reload-listener":
            log.info("Instruments reload signal received — restarting collectors")
        elif t.exception():
            log.error("Task %s raised: %s", name, t.exception())

    # Cancel and clean up all remaining tasks
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def main():
    logging.basicConfig(level=settings.log_level)
    await init_db()
    await seed_instruments_from_config()
    await get_redis()

    try:
        while True:
            symbols, db_aliases = await load_enabled_symbols()
            await _run_collectors(symbols, db_aliases)
            # Brief pause before restarting so Redis publish settles
            await asyncio.sleep(1)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
