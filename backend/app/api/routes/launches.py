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

# Fields (in priority order) that may contain the listing timestamp (ms)
_LISTING_DATE_FIELDS = (
    "onboardDate",   # Binance
    "listTime",      # OKX
    "createTime",    # MEXC
    "launched_at",
)

_HL_PREFIX = re.compile(r"^(XYZ|CASH|FLX|KM)-", re.IGNORECASE)
_EXCHANGES = ["binance", "okx", "mexc", "hyperliquid"]


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
        if not (mkt.get("swap") or ":" in symbol):
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


@router.get("/")
async def get_launches():
    """
    Fetch all non-crypto perpetual swap markets from every tracked exchange.
    Returns a flat list sorted by listed_at descending (newest first, nulls last).
    """
    results = await asyncio.gather(*[_fetch_one(ex) for ex in _EXCHANGES])
    all_rows: list[dict] = [row for batch in results for row in batch]

    all_rows.sort(
        key=lambda r: (r["listed_at"] is None, r["listed_at"] or "", r["base"]),
        reverse=False,
    )
    all_rows.reverse()

    return all_rows
