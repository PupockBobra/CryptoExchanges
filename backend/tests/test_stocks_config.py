"""Equity-perp classification (stocks/config.py).

A regression here silently corrupts the stock-volume universe: wrong canon()
splits one company across two tickers; wrong is_equity() drops a venue.
"""

from app.stocks.config import EXCLUDE, canon, is_equity


def test_canon_strips_hyperliquid_prefix():
    assert canon("XYZ-NVDA") == "NVDA"


def test_canon_strips_bybit_mexc_stock_suffix():
    assert canon("AMDSTOCK") == "AMD"
    assert canon("CATSTOCK") == "CAT"


def test_canon_applies_cross_exchange_aliases():
    assert canon("NOKIA") == "NOK"
    assert canon("SMSN") == "SAMSUNG"
    assert canon("SKHX") == "SKHYNIX"


def test_canon_is_case_insensitive_and_composable():
    # hyperliquid prefix + alias in one pass
    assert canon("xyz-nokia") == "NOK"
    assert canon("nvda") == "NVDA"


def test_canon_passthrough_for_plain_tickers():
    assert canon("TSLA") == "TSLA"


def test_exclude_covers_etfs_and_commodities():
    # ETFs/indices/commodities must never enter the company-stock universe
    for not_a_company in ("QQQ", "SPY", "GOLD", "VIX", "EUR"):
        assert not_a_company in EXCLUDE


def test_is_equity_binance_flag():
    assert is_equity("binance", {"info": {"underlyingType": "EQUITY"}})
    assert not is_equity("binance", {"info": {"underlyingType": "COIN"}})


def test_is_equity_bybit_okx_flags():
    assert is_equity("bybit", {"info": {"symbolType": "stock"}})
    assert not is_equity("bybit", {"info": {"symbolType": "perp"}})
    assert is_equity("okx", {"info": {"instCategory": "3"}})
    assert is_equity("okx", {"info": {"instCategory": 3}})   # int form too
    assert not is_equity("okx", {"info": {"instCategory": "1"}})


def test_is_equity_mexc_and_hyperliquid_by_base():
    assert is_equity("mexc", {"base": "AMDSTOCK", "info": {}})
    assert not is_equity("mexc", {"base": "BTC", "info": {}})
    assert is_equity("hyperliquid", {"base": "XYZ-NVDA", "info": {}})
    assert not is_equity("hyperliquid", {"base": "BTC", "info": {}})


def test_is_equity_handles_missing_info_and_unknown_exchange():
    assert not is_equity("binance", {"base": "X", "info": None})
    assert not is_equity("kraken", {"base": "AMDSTOCK", "info": {}})
