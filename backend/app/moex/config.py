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
}

# Internal DB key → canonical symbol in the arbitrage tracker
ASSET_TO_CANONICAL: dict[str, str] = {
    "BR": "BRN/USDT:USDT",
    "NG": "NATGAS/USDT:USDT",
    "GD": "XAU/USDT:USDT",
    "SV": "XAG/USDT:USDT",
    "PT": "XPT/USDT:USDT",
    "PD": "XPD/USDT:USDT",
}
