"""
MOEX FORTS configuration.

USDRUBF is the perpetual USD/RUB swap — its SECID never changes.

The ETL uses ISS ASSETCODE-level discovery to find all contracts (including
expired ones) without a hardcoded series list.  ISS ASSETCODE values differ
from the short internal codes used as DB keys (e.g. "GD" vs "GOLD").
"""

USDRUBF_SECID = "USDRUBF"

# Internal DB key → ISS ASSETCODE (used by the discovery query)
# Verified via /iss/engines/futures/markets/forts/securities/{SECID}.json
ASSET_ISS_CODE: dict[str, str] = {
    "BR": "BR",
    "NG": "NG",
    "GD": "GOLD",
    "SV": "SILV",
    "PT": "PLT",
    "PD": "PLD",
    "NASD": "NASD",   # NASDAQ-100 index futures
    "SPYF": "SPYF",   # S&P 500 index futures
    # Crypto index futures (ISS ASSETCODE == ticker)
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "XRP": "XRP",
    "TRX": "TRX",
}

# ISS ASSETCODEs of mini contracts whose turnover/OI is summed into the parent
# asset (added 31.07.2026).  Their SECIDs use different prefixes (BRM→BMxx,
# NGM→NRxx, GOLDM→GNxx, SILVM→S1xx), but discovery filters by the ISS
# `assetcode` param, so the ETLs just fetch each code and add the totals.
ASSET_ISS_MINI_CODE: dict[str, str] = {
    "BR": "BRM",     # Brent mini
    "NG": "NGM",     # Natural gas mini
    "GD": "GOLDM",   # Gold mini
    "SV": "SILVM",   # Silver mini
}


def iss_codes_for(asset_code: str) -> tuple[str, ...]:
    """All ISS ASSETCODEs contributing to one internal asset (main + mini)."""
    mini = ASSET_ISS_MINI_CODE.get(asset_code)
    return (ASSET_ISS_CODE[asset_code], mini) if mini else (ASSET_ISS_CODE[asset_code],)


# Internal DB key → canonical symbol in the arbitrage tracker
ASSET_TO_CANONICAL: dict[str, str] = {
    "BR": "BRN/USDT:USDT",
    "NG": "NATGAS/USDT:USDT",
    "GD": "XAU/USDT:USDT",
    "SV": "XAG/USDT:USDT",
    "PT": "XPT/USDT:USDT",
    "PD": "XPD/USDT:USDT",
    "NASD": "QQQ/USDT:USDT",   # NASDAQ-100 → Invesco QQQ
    "SPYF": "SPY/USDT:USDT",   # S&P 500 → SPDR SPY
    # Crypto index futures → canonical crypto symbols (stack with crypto perps)
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "SOL": "SOL/USDT",
    "XRP": "XRP/USDT",
    "TRX": "TRX/USDT",
}

# Assets whose FORTS open interest is NOT collected: XRP/TRX exist only on MOEX,
# so their OI cards carried a single tiny bar with nothing to compare against.
# Turnover for them is unaffected — this only gates app/moex/oi_etl.py.
OI_EXCLUDED_ASSETS: frozenset[str] = frozenset({"XRP", "TRX"})

# ── Crypto-index futures order-book spread (Order Book page overlay) ───────────
# Finam serves MOEX FORTS derivatives under MIC "RTSX" as "<SECID>@RTSX".  The
# front-month SECID rolls monthly, so it is resolved dynamically from ISS
# (see moex/fetcher.resolve_front_secids) rather than hard-coded.
MOEX_FORTS_MIC = "RTSX"

# SPB crypto ticker → (ISS assetcode, fallback lot).  The MOEX spread line is
# overlaid on the matching SPB Order Book card, so the key is the SPB ticker.
# Lots are coins-per-contract and feed the spread math exactly like the SPB lot
# (price_usd × size × lot × usdrub).
#
# The lot is COMPUTED dynamically at runtime from turnover — VALUE/(VOLUME × price
# × usdrub), snapped to 1 significant figure — in moex/fetcher.resolve_front_secids,
# refreshed daily with the front-month SECID.  The values below are the last-known
# turnover-derived lots, used only as a fallback if that computation fails.
MOEX_CRYPTO_FUTURES: dict[str, tuple[str, float]] = {
    "BTCUSDperpA": ("BTC", 0.001),
    "ETHUSDperpA": ("ETH", 0.01),
    "SOLUSDperpA": ("SOL", 1.0),
    "XRPUSDperpA": ("XRP", 100.0),
    "TRXUSDperpA": ("TRX", 100.0),
}
