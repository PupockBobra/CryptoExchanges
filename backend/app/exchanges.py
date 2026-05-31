"""
Single source of truth for exchange ccxt classes and perp market types.

Replaces three duplicate copies that previously lived in:
  backfill/ohlcv.py, backfill/funding.py, api/routes/launches.py
"""

import ccxt.async_support as ccxt_async


EXCHANGE_CLS: dict[str, type] = {
    "binance":     ccxt_async.binance,
    "okx":         ccxt_async.okx,
    "bybit":       ccxt_async.bybit,
    "mexc":        ccxt_async.mexc,
    "hyperliquid": ccxt_async.hyperliquid,
}

# defaultType to use for perpetual contracts on each exchange
PERP_MARKET_TYPE: dict[str, str] = {
    "binance":     "future",   # USDT-margined perpetuals
    "okx":         "swap",
    "bybit":       "linear",
    "mexc":        "swap",
    "hyperliquid": "swap",     # USDC-margined perpetuals
}

# Funding rate settlement interval per exchange
FUNDING_INTERVAL_HOURS: dict[str, int] = {
    "binance":     8,
    "okx":         8,
    "bybit":       8,
    "mexc":        8,
    "hyperliquid": 1,
}


def make_exchange(exchange_id: str, market_type: str | None = None, timeout_ms: int = 20_000) -> ccxt_async.Exchange:
    """
    Construct a ccxt async client for the given exchange.
    If market_type is None, defaults to the perp type for that exchange.
    """
    cls = EXCHANGE_CLS[exchange_id]
    return cls({
        "enableRateLimit": True,
        "options": {"defaultType": market_type or PERP_MARKET_TYPE[exchange_id]},
        "timeout": timeout_ms,
    })
