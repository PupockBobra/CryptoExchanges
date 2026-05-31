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

# Fields (in priority order) that may contain the listing timestamp
# Values are either millisecond integers or ISO-8601 strings.
_LISTING_DATE_FIELDS = (
    "onboardDate",            # Binance (ms int)
    "listTime",               # OKX (ms int)
    "createTime",             # MEXC (ms int)
    "launched_at",
    "lastGrowthModeChangeTime",  # Hyperliquid (ISO-8601 str) — proxy for launch date
)

_HL_PREFIX = re.compile(r"^(XYZ|CASH|FLX|KM)-", re.IGNORECASE)
_EXCHANGES = ["binance", "okx", "mexc", "hyperliquid"]


def _extract_listed_at(info: dict) -> str | None:
    for field in _LISTING_DATE_FIELDS:
        val = info.get(field)
        if not val:
            continue
        # ISO-8601 string (Hyperliquid lastGrowthModeChangeTime)
        if isinstance(val, str) and "T" in val:
            try:
                return val[:10]   # 'YYYY-MM-DD'
            except Exception:
                pass
        # Millisecond integer (Binance, OKX, MEXC)
        else:
            try:
                ts_s = int(val) / 1000
                return datetime.fromtimestamp(ts_s, tz=timezone.utc).date().isoformat()
            except Exception:
                pass
    return None


async def _fetch_one(exchange_id: str) -> list[dict]:
    try:
        import ccxt.async_support as ccxt_a
        klass = getattr(ccxt_a, exchange_id)
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


@router.get("/")
async def get_launches():
    """
    Fetch all non-crypto perpetual swap markets from every tracked exchange.
    Adds `known_since` field: earliest date we have OHLCV data for this
    (symbol, exchange) pair. Frontend uses this to detect genuinely new
    listings vs. instruments with stale/updated metadata from the exchange.
    """
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

    return all_rows
