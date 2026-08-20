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
    BACKFILL_SINCE, FLAG_EXCHANGES, STOCK_EXCHANGES, EXCLUDE, canon_market, is_equity,
)

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 2          # re-fetch recent days each run (fills the current day)
_THROTTLE_SEC = 0.15       # gentle pacing between per-instrument fetches
_EMPTY_SKIP_MS = 60 * 86_400_000   # advance `since` past an empty pre-launch window


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
            b = canon_market(e, m)
            if b not in EXCLUDE:
                ref.add(b)
    all_markets = dict(flag_markets)
    for e in STOCK_EXCHANGES:
        if e not in all_markets:
            all_markets[e] = await _equity_markets(e)

    universe: dict[str, list[tuple]] = {}
    for e in STOCK_EXCHANGES:
        insts = []
        for sym, m in all_markets[e].items():
            b = canon_market(e, m)
            if b in EXCLUDE:
                continue
            # Namespace-less venues (Hyperliquid, Bitget) tag a broad set of
            # real-world assets; keep only tickers the flag exchanges classify as
            # company stocks, dropping commodities/metals/indices.
            if e in ("hyperliquid", "bitget") and b not in ref:
                continue
            cs = (m.get("contractSize") or 1) if e == "mexc" else 1
            insts.append((sym, b, cs))
        universe[e] = insts
        log.info("Stock ETL: %s → %d company-stock perps", e, len(insts))
    return universe


# Last successfully discovered universe, shared with the OI collector.
# Discovery is a `load_markets()` per exchange; running it twice within seconds
# (this ETL, then the OI collector) is what tips Hyperliquid into 429 and leaves
# the OI collector with no stock universe at all.
_UNIVERSE_TTL = timedelta(hours=6)
_last_universe: dict[str, list[tuple]] = {}
_last_universe_at: datetime | None = None


async def build_stock_universe(max_age: timedelta = _UNIVERSE_TTL) -> dict[str, list[tuple]]:
    """
    The equity-perp universe, reusing the last discovery while it is fresh.

    The OI collector calls this to resolve exchange symbols; sharing the cache
    with the ETL keeps `load_markets()` to one sweep per cycle.
    """
    global _last_universe, _last_universe_at

    now = datetime.now(tz=timezone.utc)
    if _last_universe and _last_universe_at and now - _last_universe_at <= max_age:
        return _last_universe

    universe = await _build_universe()
    _last_universe, _last_universe_at = universe, now
    return universe


async def _fetch_exchange(exchange_id: str, insts: list[tuple], since_ms: int,
                          now_ms: int, out: dict) -> None:
    """Fetch daily OHLCV per instrument and accumulate quote_usd by (date, ticker)."""
    ex = make_exchange(exchange_id, "swap")
    try:
        for sym, ticker, cs in insts:
            # Paginate by advancing `since`: some venues (Bitget) cap daily klines
            # at ~90 bars/request regardless of limit, so one fetch would truncate
            # the history.  Advance until the newest bar stops moving forward.
            cur = since_ms
            prev_last = None
            while cur < now_ms:
                try:
                    bars = await ex.fetch_ohlcv(sym, "1d", since=cur, limit=1000)
                except Exception as exc:
                    log.debug("Stock ETL: %s %s fetch failed: %s", exchange_id, sym, exc)
                    break
                if not bars:
                    # Bitget returns an empty page when `since` predates the
                    # contract's launch; skip the window forward instead of
                    # stopping so late-listed perps still backfill.
                    cur += _EMPTY_SKIP_MS
                    continue
                for bar in bars:
                    ts, _o, _h, _l, close, vol = bar[:6]
                    if ts > now_ms or ts < since_ms or not close or not vol:
                        continue
                    d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
                    out[(d, exchange_id, ticker)] = (
                        out.get((d, exchange_id, ticker), 0.0) + close * vol * cs
                    )
                last = bars[-1][0]
                next_cur = last + 1
                if last >= now_ms or (prev_last is not None and last <= prev_last) or next_cur <= cur:
                    break
                prev_last = last
                cur = next_cur
                await asyncio.sleep(_THROTTLE_SEC)
            await asyncio.sleep(_THROTTLE_SEC)
    finally:
        await ex.close()


async def run_stock_etl() -> None:
    """One full ETL pass.  Errors are caught so the loop never crashes."""
    log.info("Stock ETL: starting pass")
    try:
        # Fresh discovery each pass, then cached for the OI collector.
        universe = await build_stock_universe(max_age=timedelta(0))
    except Exception as exc:
        log.warning("Stock ETL: universe discovery failed: %s", exc)
        return

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    floor = date.fromisoformat(BACKFILL_SINCE)

    async def _fetch(e: str) -> None:
        # Per-exchange floor: a newly-added venue (its table empty) backfills the
        # full history from BACKFILL_SINCE instead of inheriting the global latest.
        latest = await get_stock_latest_date(e)
        since_dt = floor if latest is None else max(floor, latest - timedelta(days=_LOOKBACK_DAYS))
        since_ms = int(datetime(since_dt.year, since_dt.month, since_dt.day,
                                tzinfo=timezone.utc).timestamp() * 1000)
        await _fetch_exchange(e, universe.get(e, []), since_ms, now_ms, acc)

    acc: dict[tuple, float] = {}
    await asyncio.gather(*[_fetch(e) for e in STOCK_EXCHANGES])

    rows = [(d, e, t, round(v, 2)) for (d, e, t), v in acc.items()]
    stored = await upsert_stock_daily_volume(rows)
    log.info("Stock ETL: pass complete — upserted %d rows", stored)


async def run_stock_etl_safe() -> None:
    """Wrapper used by the manual refresh endpoint."""
    try:
        await run_stock_etl()
    except Exception as exc:  # noqa: BLE001
        log.warning("Stock ETL: manual refresh failed: %s", exc)
