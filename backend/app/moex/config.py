"""
MOEX FORTS series configuration.

Update SERIES_BY_ASSET quarterly when new contracts become front-month.
USDRUBF is the perpetual USD/RUB swap — its SECID never changes.
"""

USDRUBF_SECID = "USDRUBF"

# asset_code → canonical symbol in the arbitrage tracker
ASSET_TO_CANONICAL: dict[str, str] = {
    "BR": "BRN/USDT:USDT",
    "NG": "NATGAS/USDT:USDT",
    "GD": "XAU/USDT:USDT",
    "SV": "XAG/USDT:USDT",
    "PT": "XPT/USDT:USDT",
    "PD": "XPD/USDT:USDT",
}

# asset_code → currently active contract SECID list (update quarterly)
SERIES_BY_ASSET: dict[str, list[str]] = {
    "BR": ["BRM6", "BRN6", "BRQ6", "BRU6", "BRV6", "BRX6", "BRZ6", "BRF7"],
    "NG": ["NGM6", "NGN6", "NGQ6", "NGU6", "NGV6", "NGX6", "NGZ6"],
    "GD": ["GDM6", "GDU6", "GDZ6", "GDH7"],
    "SV": ["SVM6", "SVU6", "SVZ6", "SVH7"],
    "PT": ["PTM6", "PTU6", "PTZ6", "PTH7"],
    "PD": ["PDM6", "PDU6", "PDZ6", "PDH7"],
}
