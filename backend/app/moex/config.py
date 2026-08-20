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
