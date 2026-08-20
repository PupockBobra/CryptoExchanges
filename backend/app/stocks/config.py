"""
Equity (stock) perpetual-futures universe on crypto exchanges.

Each exchange flags its equity perps differently; we normalise base tickers to a
canonical form so the same company aggregates across venues:
  - Binance:     info.underlyingType in {EQUITY, PREMARKET (pre-IPO), KR_EQUITY}
  - OKX:         info.instCategory  == '3'
  - Bybit:       info.symbolType    == 'stock'   (bases like AMDSTOCK/CATSTOCK/NOKIA)
  - MEXC:        info.conceptPlate has 'mc-trade-zone-Stock' but NOT
                 'mc-trade-zone-stockindex' (the zone also holds ETFs/indices —
                 those carry 'stockindex').  The older <TICKER>STOCK_USDT name rule
                 missed brand-named perps like TESLA/COINBASE/ROBINHOOD/OPENAI/
                 ANTHROPIC/NVIDIA.  Canonical ticker comes from info.baseCoinName
                 (TESLA→TSLA, COINBASE→COIN, NVIDIA→NVDA, …).
  - Hyperliquid: 'XYZ-' prefix, then cross-referenced to the stock base set of the
                 flag-based exchanges (its XYZ- namespace also holds commodities/FX/
                 indices, which we drop).
  - Bitget:      info.isRwa == 'YES' (marks all real-world assets), then
                 cross-referenced to the flag-exchange stock set so commodities/
                 metals (also isRwa) drop out and only company stocks remain.

Only *company* stocks are kept (incl. pre-IPO synthetics such as SPCX/OpenAI);
ETFs, index and FX/commodity tickers are excluded.
"""

# Company-stock universe starts here (also the ETL backfill floor).
BACKFILL_SINCE = "2026-01-01"

# How many equity perps the US Market section shows (Weekly Performance / Daily
# Volume / Open Interest) — the largest by turnover over the last complete ISO
# week.  Also the set the OI collector tracks, so all three pages agree.
TOP_STOCKS_DISPLAYED = 10

# Korean names have their own display section, so they never take a US Market
# slot.  `SKHY` is the ADR-style contract the venues list next to the local
# `SKHYNIX` one (they flag it EQUITY, not KR_EQUITY) — same company, so it
# belongs with the Korean cards rather than among the US stocks.
KOREAN_TICKERS = ("SKHYNIX", "SAMSUNG", "HYUNDAI", "SKHY")

# Stock-ETL tickers to serve regardless of the US Market ranking (they render in
# another section).  Without this their rows would never reach the charts, since
# the volume queries only ask for the top-N tickers.
EXTRA_DISPLAYED_TICKERS = ("SKHY",)

# Exchanges that expose an equity-perp flag directly (used to build the reference
# base set that Hyperliquid is filtered against).
FLAG_EXCHANGES = ["binance", "okx", "bybit", "mexc"]
STOCK_EXCHANGES = ["binance", "okx", "bybit", "mexc", "hyperliquid", "bitget"]

# Not company stocks — ETFs, index funds, indices, FX and commodities that slip
# through the per-exchange equity flags.
EXCLUDE: frozenset[str] = frozenset({
    # ETFs / leveraged / sector / country funds
    "QQQ", "TQQQ", "SQQQ", "SPY", "IWM", "SOXL", "UVXY", "XLE", "EWJ", "EWY",
    "EWZ", "EWT", "URNM", "KORU", "SMH", "USO", "SHLD",
    # indices
    "STXX", "SP500", "JP225", "KR200", "NIFTY", "IBOV", "VIX", "DXY", "XYZ100",
    "H100", "MUU",
    # ETF, though every venue flags it EQUITY (memory-chip basket, not a company)
    "DRAM",
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


def canon_market(exchange_id: str, market: dict) -> str:
    """
    Canonical company ticker for a market.  On MEXC the swap *base* may be a brand
    name (TESLA/COINBASE/ROBINHOOD), so prefer ``info.baseCoinName`` which is the
    real ticker (TSLA/COIN/HOOD); every other venue uses the market base.
    """
    if exchange_id == "mexc":
        info = market.get("info", {}) or {}
        return canon(info.get("baseCoinName") or market.get("base", ""))
    return canon(market.get("base", ""))


def is_equity(exchange_id: str, market: dict) -> bool:
    """True if a ccxt swap market is an equity perp on the given exchange."""
    info = market.get("info", {}) or {}
    base = (market.get("base") or "").upper()
    if exchange_id == "binance":
        # EQUITY = listed company; PREMARKET = pre-IPO company (OPENAI/ANTHROPIC);
        # KR_EQUITY = Korean stock (SAMSUNG/SKHYNIX/HYUNDAI).  COMMODITY/INDEX/COIN
        # are not company stocks.
        return info.get("underlyingType") in ("EQUITY", "PREMARKET", "KR_EQUITY")
    if exchange_id == "bybit":
        return info.get("symbolType") == "stock"
    if exchange_id == "okx":
        return str(info.get("instCategory")) == "3"
    if exchange_id == "mexc":
        plates = info.get("conceptPlate") or []
        # The Stock zone also holds ETFs/leveraged funds/indices — every one of
        # those carries the 'stockindex' zone, single-company stocks never do.
        return "mc-trade-zone-Stock" in plates and "mc-trade-zone-stockindex" not in plates
    if exchange_id == "hyperliquid":
        return base.startswith("XYZ-")
    if exchange_id == "bitget":
        # Bitget tags every real-world asset (stocks, commodities, metals) with
        # info.isRwa == 'YES' and plain crypto with 'NO'.  This selects all RWA;
        # _build_universe then cross-references against the flag-exchange equity
        # set so only company stocks survive (commodities/metals are dropped).
        return info.get("isRwa") == "YES"
    return False
