"""
Equity-perp volume ETL.

Every 6 h (and once at startup) it scans each crypto exchange for company-stock
perpetuals, pulls daily OHLCV, and stores per-day turnover in USD into
``stock_daily_volume`` (date × exchange × canonical ticker).  USD→RUB conversion
happens at query time via the shared ``moex_fx_rates`` table.

Turnover per day = close × volume × contractSize (contractSize only matters on
MEXC; it is 1 on the other venues).  MEXC daily klines emit placeholder future
bars — those (ts > now) are skipped.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from app.exchanges import make_exchange
from app.db.timescale import get_stock_latest_date, upsert_stock_daily_volume
from app.stocks.config import (
    BACKFILL_SINCE, FLAG_EXCHANGES, STOCK_EXCHANGES, EXCLUDE, canon, is_equity,
)

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 2          # re-fetch recent days each run (fills the current day)
_THROTTLE_SEC = 0.15       # gentle pacing between per-instrument fetches


async def stock_etl_loop() -> None:
    """Background task: run once at startup then every 6 h."""
    while True:
        try:
            await run_stock_etl()
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.warning("Stock ETL: loop iteration failed: %s", exc)
        await asyncio.sleep(21_600)


async def _equity_markets(exchange_id: str) -> dict:
    """Return {symbol: market} of equity swap markets on the exchange."""
    ex = make_exchange(exchange_id, "swap")
    try:
        markets = await ex.load_markets()
    finally:
        await ex.close()
    return {
        s: m for s, m in markets.items()
        if m.get("type") == "swap" and is_equity(exchange_id, m)
    }


async def _build_universe() -> dict[str, list[tuple]]:
    """
    Discover company-stock instruments per exchange.
    Returns {exchange: [(symbol, canon_ticker, contract_size), ...]}.
    Hyperliquid is filtered to tickers present on the flag-based exchanges.
    """
    flag_markets = {e: await _equity_markets(e) for e in FLAG_EXCHANGES}
    ref: set[str] = set()
    for e in FLAG_EXCHANGES:
        for m in flag_markets[e].values():
            b = canon(m.get("base", ""))
            if b not in EXCLUDE:
                ref.add(b)
    all_markets = dict(flag_markets)
    all_markets["hyperliquid"] = await _equity_markets("hyperliquid")

    universe: dict[str, list[tuple]] = {}
    for e in STOCK_EXCHANGES:
        insts = []
        for sym, m in all_markets[e].items():
            b = canon(m.get("base", ""))
            if b in EXCLUDE:
                continue
            if e == "hyperliquid" and b not in ref:
                continue
            cs = (m.get("contractSize") or 1) if e == "mexc" else 1
            insts.append((sym, b, cs))
        universe[e] = insts
        log.info("Stock ETL: %s → %d company-stock perps", e, len(insts))
    return universe


async def _fetch_exchange(exchange_id: str, insts: list[tuple], since_ms: int,
                          now_ms: int, out: dict) -> None:
    """Fetch daily OHLCV per instrument and accumulate quote_usd by (date, ticker)."""
    ex = make_exchange(exchange_id, "swap")
    try:
        for sym, ticker, cs in insts:
            try:
                bars = await ex.fetch_ohlcv(sym, "1d", since=since_ms, limit=1000)
            except Exception as exc:
                log.debug("Stock ETL: %s %s fetch failed: %s", exchange_id, sym, exc)
                continue
            for bar in bars:
                ts, _o, _h, _l, close, vol = bar[:6]
                if ts > now_ms or ts < since_ms or not close or not vol:
                    continue
                d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
                out[(d, exchange_id, ticker)] = (
                    out.get((d, exchange_id, ticker), 0.0) + close * vol * cs
                )
            await asyncio.sleep(_THROTTLE_SEC)
    finally:
        await ex.close()


async def run_stock_etl() -> None:
    """One full ETL pass.  Errors are caught so the loop never crashes."""
    log.info("Stock ETL: starting pass")
    try:
        universe = await _build_universe()
    except Exception as exc:
        log.warning("Stock ETL: universe discovery failed: %s", exc)
        return

    latest = await get_stock_latest_date()
    if latest is None:
        since_dt = date.fromisoformat(BACKFILL_SINCE)
    else:
        since_dt = max(date.fromisoformat(BACKFILL_SINCE),
                       latest - timedelta(days=_LOOKBACK_DAYS))
    since_ms = int(datetime(since_dt.year, since_dt.month, since_dt.day,
                            tzinfo=timezone.utc).timestamp() * 1000)
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    acc: dict[tuple, float] = {}
    await asyncio.gather(*[
        _fetch_exchange(e, universe.get(e, []), since_ms, now_ms, acc)
        for e in STOCK_EXCHANGES
    ])

    rows = [(d, e, t, round(v, 2)) for (d, e, t), v in acc.items()]
    stored = await upsert_stock_daily_volume(rows)
    log.info("Stock ETL: pass complete — upserted %d rows (from %s)", stored, since_dt)


async def run_stock_etl_safe() -> None:
    """Wrapper used by the manual refresh endpoint."""
    try:
        await run_stock_etl()
    except Exception as exc:  # noqa: BLE001
        log.warning("Stock ETL: manual refresh failed: %s", exc)
