"""
GET /api/launches — scan all exchanges for non-crypto perpetual futures.

Returns a flat list of swap markets whose base asset is a real-world
instrument (commodity, metal, stock, index).  Listing dates are extracted
from exchange-specific market.info fields where available.

Used by the frontend Launches page (on-demand, triggered by Refresh button).
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter
from app.db.timescale import get_pool
from app.exchanges import EXCHANGE_CLS

log = logging.getLogger(__name__)

router = APIRouter()

# Real-world (non-crypto) base assets known to trade as perps on crypto exchanges.
# Hyperliquid prefixes (XYZ-, CASH-, FLX-, KM-) are stripped before matching.
NON_CRYPTO_BASES: frozenset[str] = frozenset({
    # Energy
    "BRN", "BZ", "BRENT", "UKOIL", "USOIL", "OIL", "WTI", "NG", "NGAS", "NATGAS",
    # Metals
    "GOLD", "XAU", "XAUT", "SILVER", "XAG", "PLATINUM", "XPT", "PALLADIUM", "XPD", "COPPER",
    # Agricultural
    "WHEAT", "CORN", "SOYBEAN", "COTTON", "COFFEE", "COCOA", "SUGAR",
    # Indices / ETFs
    "QQQ", "SPY", "SPX", "SPX500", "NAS100", "NASDAQ", "NDX", "DOW", "DJI", "NIKKEI",
    "DAX", "FTSE", "CAC", "ES", "NQ", "RUT", "VIX",
    # US Stocks
    "AAPL", "AMZN", "GOOGL", "GOOG", "META", "MSFT", "NVDA", "TSLA", "NFLX",
    "AMD", "INTC", "QCOM", "MU", "TXN", "AVGO", "CRM", "ORCL", "IBM", "CSCO",
    "PYPL", "COIN", "HOOD", "PLTR", "ABNB", "UBER", "LYFT", "SNAP", "PINS",
    "RBLX", "DIS", "V", "MA", "JPM", "BAC", "GS", "MS", "WMT", "TGT", "COST",
    "HD", "PFE", "MRNA", "JNJ", "UNH", "CVX", "XOM", "BA", "GE", "F", "GM",
    "NIO", "BABA", "JD", "PDD", "SHOP", "SQ", "ROKU", "ZM", "CRWD", "DDOG",
    "SNOW", "AFRM", "SOFI", "RIVN", "LCID", "DELL", "HPQ", "SBUX", "MCD",
    "KO", "PEP", "PG", "JNJ", "LLY", "ABBV", "MRK", "BMY",
})

# Fields (in priority order) that may contain the listing timestamp (ms int).
# Hyperliquid does not expose a reliable listing date through its API —
# lastGrowthModeChangeTime reflects parameter changes, not the launch date.
_LISTING_DATE_FIELDS = (
    "onboardDate",   # Binance
    "listTime",      # OKX
    "launchTime",    # Bybit
    "createTime",    # MEXC
    "launched_at",
)

_HL_PREFIX = re.compile(r"^(XYZ|CASH|FLX|KM)-", re.IGNORECASE)
_EXCHANGES = ["binance", "okx", "mexc", "bybit"]


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


async def _fetch_one(exchange_id: str) -> list[dict]:
    try:
        klass = EXCHANGE_CLS[exchange_id]
        ex = klass({"options": {"defaultType": "swap"}})
        try:
            markets = await ex.load_markets()
        finally:
            await ex.close()
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

    return rows


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
    _ALIASES = {"XAUT": "XAU", "NGAS": "NATGAS", "UKOIL": "BRN", "USOIL": "WTI"}
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


_REFRESH_INTERVAL_S = 3600  # refresh cache every hour

_cache: list[dict] = []
_cache_updated_at: datetime | None = None
_refresh_lock = asyncio.Lock()


async def _do_refresh() -> None:
    """
    Replace _cache atomically. Lock prevents concurrent rebuilds
    (e.g. background loop + POST /refresh racing) and torn reads.
    """
    global _cache, _cache_updated_at
    async with _refresh_lock:
        exchange_rows, known_since = await asyncio.gather(
            asyncio.gather(*[_fetch_one(ex) for ex in _EXCHANGES]),
            _fetch_known_since(),
        )
        all_rows: list[dict] = []
        for batch in exchange_rows:
            for row in batch:
                row["known_since"] = known_since.get(row["base"])
                all_rows.append(row)
        all_rows.sort(
            key=lambda r: (r["listed_at"] is None, r["listed_at"] or "", r["base"]),
            reverse=False,
        )
        all_rows.reverse()
        _cache = all_rows
        _cache_updated_at = datetime.now(timezone.utc)
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
