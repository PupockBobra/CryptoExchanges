"""
Top-N crypto perpetuals per exchange — universe rules.

The Cryptocurrencies slice of the asset-group charts used to be the three curated
majors (BTC/ETH/SOL) while the other two slices covered their whole universe, so
crypto was structurally understated.  This module picks, per exchange, the N
perpetuals with the largest 24-hour turnover.

"Crypto" here means what is left after removing everything the other slices
already own: equity perps (flagged per venue by ``stocks.config.is_equity``) and
the commodity / metal / index / stock tickers of ``NON_CRYPTO_BASES``.
"""

from app.api.routes.launches import NON_CRYPTO_BASES
from app.stocks.config import is_equity

# How many perps per exchange enter the ranking.
TOP_N = 100

# Daily history floor — matches the stock ETL so the Weekly (YTD) view has both
# slices over the same span.
BACKFILL_SINCE = "2026-01-01"

# The venues that carry crypto perps (same six as the stock universe).
CRYPTO_EXCHANGES = ["binance", "okx", "bybit", "mexc", "hyperliquid", "bitget"]

# Settlement quotes we count.  A perp quoted in anything else (rare, e.g. coin-
# margined contracts) reports turnover in a different unit and would not be
# comparable with the USD-denominated rest of the page.
_QUOTES = {"USDT", "USDC", "USD", "USDT0"}


def base_of(market: dict) -> str:
    """
    Canonical base ticker for the non-crypto check.

    Hyperliquid hosts builder DEXes whose bases are namespaced (`XYZ-CL`,
    `CASH-NVDA`, `FLX-GOLD`), so the prefix has to come off before the base can
    be compared with the allow-lists — otherwise every one of them reads as an
    unknown token and passes as "crypto".
    """
    base = (market.get("base") or "").upper()
    return base.split("-")[-1] if "-" in base else base


def is_crypto_perp(exchange_id: str, market: dict) -> bool:
    """True for a perpetual on a genuine cryptocurrency."""
    if not market.get("swap") or market.get("type") != "swap":
        return False
    if (market.get("quote") or "").upper() not in _QUOTES:
        return False
    if is_equity(exchange_id, market):
        return False
    base = base_of(market)
    if not base or base in NON_CRYPTO_BASES:
        return False
    # A namespaced base that survived the checks above is a builder-DEX listing
    # of some real-world asset we simply have not enumerated — the venue's plain
    # crypto perps are never namespaced, so treat the prefix as disqualifying.
    return "-" not in (market.get("base") or "")


def turnover_24h(ticker: dict) -> float:
    """
    24-hour quote turnover from a ccxt ticker, falling back to base × last.

    Not every venue fills `quoteVolume`; without the fallback those markets rank
    as zero and the whole exchange collapses to the handful that do fill it.
    """
    if not ticker:
        return 0.0
    quote_vol = ticker.get("quoteVolume")
    if quote_vol:
        return float(quote_vol)
    base_vol = ticker.get("baseVolume") or 0
    last = ticker.get("last") or ticker.get("close") or 0
    return float(base_vol) * float(last)


def rank_top(markets: dict, tickers: dict, exchange_id: str, top_n: int = TOP_N) -> list[tuple]:
    """
    Rank an exchange's crypto perps by 24h turnover.

    Returns [(symbol, canonical_base, contract_size), …], longest turnover first,
    capped at `top_n`.  contractSize matters on MEXC only, exactly as in the
    other volume ETLs.
    """
    scored = []
    for symbol, market in markets.items():
        if not is_crypto_perp(exchange_id, market):
            continue
        cs = float(market.get("contractSize") or 1) if exchange_id == "mexc" else 1.0
        scored.append((turnover_24h(tickers.get(symbol, {})), symbol, base_of(market), cs))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [(sym, base, cs) for _v, sym, base, cs in scored[:top_n]]
