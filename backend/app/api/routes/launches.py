"""
GET /api/launches — scan all exchanges for non-crypto perpetual futures.

Returns a flat list of swap markets whose base asset is a real-world
instrument (commodity, metal, stock, index).  Listing dates are extracted
from exchange-specific market.info fields where available, and derived from
the first traded daily candle on the exchanges that publish no usable field
(Bitget, Hyperliquid — see _DERIVED_DATE_EXCHANGES).

Used by the frontend Launches page (on-demand, triggered by Refresh button).
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter
from app.db.timescale import (
    fetch_launch_first_trades,
    get_pool,
    upsert_launch_first_trades,
)
from app.exchanges import EXCHANGE_CLS

log = logging.getLogger(__name__)

router = APIRouter()

# Real-world (non-crypto) base assets known to trade as perps on crypto exchanges.
# Hyperliquid builder-DEX prefixes (XYZ-, CASH-, …) are stripped before matching.
#
# Deliberately NOT listed — crypto tokens whose ticker collides with a
# real-world one (verified by price on 2026-07-29):
#   SPX → SPX6900 memecoin ($0.32), not the S&P 500 (that is SPX500 on MEXC)
#   CVX → Convex Finance ($1.33), not Chevron
#   F   → an unrelated token ($0.0029), not Ford
NON_CRYPTO_BASES: frozenset[str] = frozenset({
    # Energy
    "BRN", "BZ", "BRENT", "UKOIL", "USOIL", "OIL", "WTI", "NG", "NGAS", "NATGAS",
    # Metals
    "GOLD", "XAU", "XAUT", "SILVER", "XAG", "PLATINUM", "XPT", "PALLADIUM", "XPD", "COPPER",
    # Agricultural
    "WHEAT", "CORN", "SOYBEAN", "COTTON", "COFFEE", "COCOA", "SUGAR",
    # Indices / ETFs
    "QQQ", "SPY", "SPX500", "NAS100", "NASDAQ", "NDX", "DOW", "DJI", "NIKKEI",
    "DAX", "FTSE", "CAC", "ES", "NQ", "RUT", "VIX",
    # US Stocks
    "AAPL", "AMZN", "GOOGL", "GOOG", "META", "MSFT", "NVDA", "TSLA", "NFLX",
    "AMD", "INTC", "QCOM", "MU", "TXN", "AVGO", "CRM", "ORCL", "IBM", "CSCO",
    "PYPL", "COIN", "HOOD", "PLTR", "ABNB", "UBER", "LYFT", "SNAP", "PINS",
    "RBLX", "DIS", "V", "MA", "JPM", "BAC", "GS", "MS", "WMT", "TGT", "COST",
    "HD", "PFE", "MRNA", "JNJ", "UNH", "XOM", "BA", "GE", "GM",
    "NIO", "BABA", "JD", "PDD", "SHOP", "SQ", "ROKU", "ZM", "CRWD", "DDOG",
    "SNOW", "AFRM", "SOFI", "RIVN", "LCID", "DELL", "HPQ", "SBUX", "MCD",
    "KO", "PEP", "PG", "JNJ", "LLY", "ABBV", "MRK", "BMY", "SPCX",
    # Korean stocks
    "SKHYNIX", "SAMSUNG", "HYUNDAI",
})

# Fields (in priority order) that may contain the listing timestamp (ms int).
# Verified against the first traded daily candle on 2026-07-29: every date
# Binance / OKX / Bybit report matches it exactly, MEXC is within 2 days.
# Bitget's `launchTime` is always empty and its `openTime` is a bulk config
# timestamp (2026-02-02 for every equity perp, months after they started
# trading), so Bitget goes through _first_traded_date instead.
_LISTING_DATE_FIELDS = (
    "onboardDate",   # Binance
    "listTime",      # OKX
    "launchTime",    # Bybit
    "createTime",    # MEXC
    "launched_at",
)

# Hyperliquid hosts several builder-deployed perp DEXs, each prefixing its
# markets (xyz:AAPL → XYZ-AAPL).  The same underlying can trade on more than
# one of them, so rows are collapsed to the earliest listing per base.
_HL_PREFIX = re.compile(r"^(XYZ|CASH|FLX|KM|MKTS|PARA|VNTL|HYNA|ABCD)-", re.IGNORECASE)
_EXCHANGES = ["binance", "okx", "mexc", "bybit", "bitget", "hyperliquid"]

# Exchanges with no usable listing field — the launch date is the first daily
# candle that actually traded.
_DERIVED_DATE_EXCHANGES = frozenset({"bitget", "hyperliquid"})

_DAY_MS = 86_400_000
# Far enough back to predate every perp; Hyperliquid answers a `since` this old
# with its full history, Bitget/OKX answer with an empty page (see below).
_EARLY_MS = int(datetime(2017, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
_WALK_STEP_MS = 60 * _DAY_MS   # < the ~90-bar page Bitget returns, so no launch is stepped over
_MAX_PROBES = 45               # 45 x 60d ≈ 7.4 years back
_PROBE_PAUSE_S = 0.25

# {(exchange_id, symbol): "YYYY-MM-DD"} — a first-trade date never changes, so
# only successful lookups are memoised (misses are retried next refresh).
# Backed by launch_first_trade in the DB: loaded once on the first refresh and
# extended as new symbols are resolved, so a restart doesn't re-walk every
# Bitget/Hyperliquid kline history (~2.5 min).
_first_trade_cache: dict[tuple[str, str], str] = {}
_first_trade_loaded = False
_first_trade_unsaved: list[tuple[str, str, str]] = []


async def _load_first_trade_cache() -> None:
    """Seed the in-memory cache from the DB (once per process)."""
    global _first_trade_loaded
    if _first_trade_loaded:
        return
    try:
        stored = await fetch_launch_first_trades()
    except Exception as exc:
        log.warning("launches: could not load stored first-trade dates: %s", exc)
        return
    # Anything already resolved in this process wins — it was just fetched live.
    _first_trade_cache.update({k: v for k, v in stored.items() if k not in _first_trade_cache})
    _first_trade_loaded = True
    log.info("launches: loaded %d stored first-trade dates", len(stored))


async def _save_first_trade_cache() -> None:
    """Persist dates resolved since the last save."""
    if not _first_trade_unsaved:
        return
    pending = list(_first_trade_unsaved)
    _first_trade_unsaved.clear()
    try:
        await upsert_launch_first_trades(pending)
        log.info("launches: stored %d newly derived first-trade dates", len(pending))
    except Exception as exc:
        _first_trade_unsaved.extend(pending)   # retry on the next refresh
        log.warning("launches: could not store first-trade dates: %s", exc)


def _extract_listed_at(info: dict) -> str | None:
    for field in _LISTING_DATE_FIELDS:
        val = info.get(field)
        if val:
            try:
                ts_s = int(val) / 1000
                return datetime.fromtimestamp(ts_s, tz=timezone.utc).date().isoformat()
            except Exception:
                pass
    return None


def _iso_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def _first_volume_date(batch: list) -> str | None:
    """Date of the first bar that traded. Hyperliquid pads the history of its
    oldest markets with zero-volume bars going back to 2020, so the first bar
    is not necessarily the first trading day."""
    for bar in batch:
        if (bar[5] or 0) > 0:
            return _iso_date(bar[0])
    return None


async def _first_traded_date(ex, symbol: str) -> str | None:
    """
    First daily candle with volume, for exchanges that publish no listing date.

    Two request patterns, because exchanges disagree about a `since` that
    predates the contract:
      * Hyperliquid returns the whole history in one page → scan it.
      * Bitget (like OKX) returns an EMPTY page and only answers for a window
        that overlaps real candles → walk backwards in 60-day steps until a
        page starts later than the `since` we asked for; that page begins at
        the launch.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        batch = await ex.fetch_ohlcv(symbol, "1d", since=_EARLY_MS, limit=5000)
    except Exception as exc:
        log.debug("launches: fetch_ohlcv %s failed: %s", symbol, exc)
        return None
    if batch:
        return _first_volume_date(batch)

    earliest_ms: int | None = None
    since_ms = now_ms - _WALK_STEP_MS
    for _ in range(_MAX_PROBES):
        await asyncio.sleep(_PROBE_PAUSE_S)
        try:
            batch = await ex.fetch_ohlcv(symbol, "1d", since=since_ms, limit=500)
        except Exception as exc:
            log.debug("launches: fetch_ohlcv %s failed: %s", symbol, exc)
            break
        if not batch:
            break   # nothing in this window and nothing older — stop with what we have
        first_ms = batch[0][0]
        if earliest_ms is None or first_ms < earliest_ms:
            earliest_ms = first_ms
        if first_ms > since_ms + _DAY_MS:
            return _first_volume_date(batch) or _iso_date(first_ms)
        since_ms -= _WALK_STEP_MS

    return _iso_date(earliest_ms) if earliest_ms else None


async def _resolve_derived_dates(ex, exchange_id: str, rows: list[dict]) -> None:
    """Fill listed_at from OHLCV for rows the exchange gave no date for.

    Sequential on purpose: this is the same rate-limit discipline the SPB /
    orderbook collectors use — a burst of parallel klines gets throttled.
    """
    for row in rows:
        if row["listed_at"]:
            continue
        key = (exchange_id, row["symbol"])
        derived = _first_trade_cache.get(key)
        if derived is None:
            derived = await _first_traded_date(ex, row["symbol"])
            if derived:
                _first_trade_cache[key] = derived
                _first_trade_unsaved.append((exchange_id, row["symbol"], derived))
        row["listed_at"] = derived


def _collapse_to_earliest(rows: list[dict]) -> list[dict]:
    """One row per base — the earliest listing wins (undated rows lose)."""
    best: dict[str, dict] = {}
    for row in rows:
        cur = best.get(row["base"])
        if cur is None:
            best[row["base"]] = row
            continue
        if row["listed_at"] and (not cur["listed_at"] or row["listed_at"] < cur["listed_at"]):
            best[row["base"]] = row
    return list(best.values())


async def _fetch_one(exchange_id: str) -> list[dict]:
    try:
        klass = EXCHANGE_CLS[exchange_id]
        ex = klass({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    except Exception as exc:
        log.warning("launches: %s client init failed: %s", exchange_id, exc)
        return []

    try:
        try:
            markets = await ex.load_markets()
        except Exception as exc:
            log.warning("launches: %s load_markets failed: %s", exchange_id, exc)
            return []

        rows: list[dict] = []
        for symbol, mkt in markets.items():
            if mkt.get("type") != "swap":
                continue

            base: str = mkt.get("base", "")
            clean = _HL_PREFIX.sub("", base).upper()
            if clean not in NON_CRYPTO_BASES:
                continue

            rows.append({
                "symbol":    symbol,
                "base":      clean,
                "exchange":  exchange_id,
                "listed_at": _extract_listed_at(mkt.get("info", {})),
            })

        if exchange_id in _DERIVED_DATE_EXCHANGES:
            await _resolve_derived_dates(ex, exchange_id, rows)
        if exchange_id == "hyperliquid":
            rows = _collapse_to_earliest(rows)

        return rows
    finally:
        try:
            await ex.close()
        except Exception:
            pass


async def _fetch_known_since() -> dict[str, str]:
    """
    Returns {base: first_ohlcv_date} aggregated across ALL exchanges.

    'base' is the part of the canonical symbol before '/' (e.g. 'XAU' from
    'XAU/USDT:USDT').  By aggregating across exchanges, we catch cases where
    an instrument is well-known on Binance/OKX even though we only recently
    added an alias for it on MEXC — avoiding false "New" flags caused by
    same-day known_since == listed_at.

    XAUT is aliased to XAU because MEXC uses XAUT as the ccxt base while
    our canonical symbols use XAU.
    """
    _ALIASES = {"XAUT": "XAU", "NGAS": "NATGAS", "UKOIL": "BRN", "USOIL": "WTI", "BZ": "BRN"}
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT SPLIT_PART(symbol, '/', 1) AS base, MIN(ts::date)::text AS first_date "
        "FROM ohlcv_daily GROUP BY SPLIT_PART(symbol, '/', 1)"
    )
    result: dict[str, str] = {}
    for r in rows:
        base = r["base"].upper()
        canon = _ALIASES.get(base, base)
        # Keep the earliest date if there are multiple aliases for the same canon base
        if canon not in result or r["first_date"] < result[canon]:
            result[canon] = r["first_date"]
    return result


def _sort_rows(rows: list[dict]) -> None:
    """Newest listings first, rows without a listing date last.

    Two stable sorts: base A→Z as tiebreaker, then date descending
    (None → "" sorts below every real date under reverse=True).
    """
    rows.sort(key=lambda r: r["base"])
    rows.sort(key=lambda r: r["listed_at"] or "", reverse=True)


_REFRESH_INTERVAL_S = 3600  # refresh cache every hour

_cache: list[dict] = []
_cache_updated_at: datetime | None = None
_refresh_lock = asyncio.Lock()

# Last successful scan per exchange. Hyperliquid answers load_markets with
# 429 often enough that without this a single throttled refresh would drop the
# whole exchange from the page until the next hourly pass.
_last_good_rows: dict[str, list[dict]] = {}


async def _do_refresh() -> None:
    """
    Replace _cache atomically. Lock prevents concurrent rebuilds
    (e.g. background loop + POST /refresh racing) and torn reads.
    """
    global _cache, _cache_updated_at
    async with _refresh_lock:
        await _load_first_trade_cache()
        exchange_rows, known_since = await asyncio.gather(
            asyncio.gather(*[_fetch_one(ex) for ex in _EXCHANGES]),
            _fetch_known_since(),
        )
        all_rows: list[dict] = []
        for exchange_id, batch in zip(_EXCHANGES, exchange_rows):
            if batch:
                _last_good_rows[exchange_id] = batch
            else:
                batch = _last_good_rows.get(exchange_id, [])
                if batch:
                    log.warning("launches: %s scan empty, keeping %d rows from the "
                                "previous refresh", exchange_id, len(batch))
            for row in batch:
                row["known_since"] = known_since.get(row["base"])
                all_rows.append(row)
        _sort_rows(all_rows)
        _cache = all_rows
        _cache_updated_at = datetime.now(timezone.utc)
        await _save_first_trade_cache()
        log.info("launches: cache updated (%d rows)", len(_cache))


async def launches_refresh_loop() -> None:
    """Background task started at server startup. Refreshes every hour."""
    while True:
        try:
            await _do_refresh()
        except Exception as exc:
            log.warning("launches: refresh failed: %s", exc)
        await asyncio.sleep(_REFRESH_INTERVAL_S)


@router.get("/")
async def get_launches():
    """Return cached non-crypto perp data. Cache is refreshed every hour."""
    if not _cache:
        # First request before the background loop has run — wait for data
        await _do_refresh()
    return {
        "data":       _cache,
        "updated_at": _cache_updated_at.isoformat() if _cache_updated_at else None,
    }


@router.post("/refresh")
async def force_refresh():
    """Force immediate cache refresh (triggered by frontend Refresh button)."""
    await _do_refresh()
    return {
        "data":       _cache,
        "updated_at": _cache_updated_at.isoformat() if _cache_updated_at else None,
    }
