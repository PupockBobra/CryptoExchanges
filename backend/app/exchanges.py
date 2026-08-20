"""
Single source of truth for exchange ccxt classes and perp market types.

Replaces three duplicate copies that previously lived in:
  backfill/ohlcv.py, backfill/funding.py, api/routes/launches.py
"""

import ccxt.async_support as ccxt_async
from ccxt.base.exchange import Exchange as _CcxtExchange


# ── ccxt keysort None-safety patch ────────────────────────────────────────────
# OKX intermittently returns a market with id=None. ccxt's set_markets() calls
# keysort(markets_by_id) → sorted(dict.items()), which raises
# "'<' not supported between instances of 'NoneType' and 'str'" and makes the
# WHOLE load_markets() fail. Since OI/funding/OHLCV backfills all call
# load_markets(), a single bad market silently breaks all OKX data collection.
# Make the sort None-safe (None keys sort last) so one bad market can't take
# down the exchange.
def _safe_keysort(dictionary):
    return dict(sorted(dictionary.items(), key=lambda kv: (kv[0] is None, kv[0])))


_CcxtExchange.keysort = staticmethod(_safe_keysort)


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

# ── Crypto majors: spot price, perpetual volume/OI ────────────────────────────
# BTC/ETH/SOL stay 'spot' instruments so the real-time price feed uses the spot
# symbol, but their daily *trading volume* and *open interest* are sourced from
# PERPETUAL futures and stored under the same canonical symbol. Only the
# perpetual contract is used (no quarterly/delivery/options). Map:
#   canonical → { exchange_id: perp symbol }
# Consumed by backend/app/backfill/ohlcv.py and backend/app/oi/etl.py.
CRYPTO_PERP_OVERRIDES: dict[str, dict[str, str]] = {
    "BTC/USDT": {
        "binance": "BTC/USDT:USDT", "okx": "BTC/USDT:USDT", "bybit": "BTC/USDT:USDT",
        "mexc": "BTC/USDT:USDT", "hyperliquid": "BTC/USDC:USDC",
    },
    "ETH/USDT": {
        "binance": "ETH/USDT:USDT", "okx": "ETH/USDT:USDT", "bybit": "ETH/USDT:USDT",
        "mexc": "ETH/USDT:USDT", "hyperliquid": "ETH/USDC:USDC",
    },
    "SOL/USDT": {
        "binance": "SOL/USDT:USDT", "okx": "SOL/USDT:USDT", "bybit": "SOL/USDT:USDT",
        "mexc": "SOL/USDT:USDT", "hyperliquid": "SOL/USDC:USDC",
    },
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
