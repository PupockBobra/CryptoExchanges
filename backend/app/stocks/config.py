"""
Equity (stock) perpetual-futures universe on crypto exchanges.

Each exchange flags its equity perps differently; we normalise base tickers to a
canonical form so the same company aggregates across venues:
  - Binance:     info.underlyingType == 'EQUITY'
  - OKX:         info.instCategory  == '3'
  - Bybit:       info.symbolType    == 'stock'   (bases like AMDSTOCK/CATSTOCK/NOKIA)
  - MEXC:        base ends with 'STOCK'  (<TICKER>STOCK_USDT, zone tradfi/Stock)
  - Hyperliquid: 'XYZ-' prefix, then cross-referenced to the stock base set of the
                 flag-based exchanges (its XYZ- namespace also holds commodities/FX/
                 indices, which we drop).

Only *company* stocks are kept (incl. pre-IPO synthetics such as SPCX/OpenAI);
ETFs, index and FX/commodity tickers are excluded.
"""

# Company-stock universe starts here (also the ETL backfill floor).
BACKFILL_SINCE = "2026-01-01"

# Exchanges that expose an equity-perp flag directly (used to build the reference
# base set that Hyperliquid is filtered against).
FLAG_EXCHANGES = ["binance", "okx", "bybit", "mexc"]
STOCK_EXCHANGES = ["binance", "okx", "bybit", "mexc", "hyperliquid"]

# Not company stocks — ETFs, index funds, indices, FX and commodities that slip
# through the per-exchange equity flags.
EXCLUDE: frozenset[str] = frozenset({
    # ETFs / leveraged / sector / country funds
    "QQQ", "TQQQ", "SQQQ", "SPY", "IWM", "SOXL", "UVXY", "XLE", "EWJ", "EWY",
    "EWZ", "EWT", "URNM", "KORU", "SMH", "USO", "SHLD",
    # indices
    "STXX", "SP500", "JP225", "KR200", "NIFTY", "IBOV", "VIX", "DXY", "XYZ100",
    "H100", "MUU",
    # HL commodities / FX / misc
    "GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM", "WHEAT", "CORN",
    "NATGAS", "BRENTOIL", "CL", "ALUMINIUM", "URANIUM", "TTF", "EUR", "GBP",
    "JPY", "KRW", "VOL", "BIRD", "PURRDAT", "GIGADEV", "SHAZ",
})

# Cross-exchange ticker aliases → canonical company ticker.
_ALIASES = {"NOKIA": "NOK", "SMSN": "SAMSUNG", "SKHX": "SKHYNIX"}


def canon(base: str) -> str:
    """Normalise an exchange base symbol to a canonical company ticker."""
    b = base.upper()
    if b.startswith("XYZ-"):
        b = b[4:]
    if b.endswith("STOCK"):
        b = b[:-5]
    return _ALIASES.get(b, b)


def is_equity(exchange_id: str, market: dict) -> bool:
    """True if a ccxt swap market is an equity perp on the given exchange."""
    info = market.get("info", {}) or {}
    base = (market.get("base") or "").upper()
    if exchange_id == "binance":
        return info.get("underlyingType") == "EQUITY"
    if exchange_id == "bybit":
        return info.get("symbolType") == "stock"
    if exchange_id == "okx":
        return str(info.get("instCategory")) == "3"
    if exchange_id == "mexc":
        return base.endswith("STOCK")
    if exchange_id == "hyperliquid":
        return base.startswith("XYZ-")
    return False
