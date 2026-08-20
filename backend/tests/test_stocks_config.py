"""Equity-perp classification (stocks/config.py).

A regression here silently corrupts the stock-volume universe: wrong canon()
splits one company across two tickers; wrong is_equity() drops a venue.
"""

from app.stocks.config import EXCLUDE, canon, canon_market, is_equity


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
    # pre-IPO companies (OPENAI/ANTHROPIC) and Korean stocks are equities too
    assert is_equity("binance", {"info": {"underlyingType": "PREMARKET"}})
    assert is_equity("binance", {"info": {"underlyingType": "KR_EQUITY"}})
    # crypto / commodities / indices are not
    assert not is_equity("binance", {"info": {"underlyingType": "COIN"}})
    assert not is_equity("binance", {"info": {"underlyingType": "COMMODITY"}})
    assert not is_equity("binance", {"info": {"underlyingType": "INDEX"}})


def test_is_equity_bybit_okx_flags():
    assert is_equity("bybit", {"info": {"symbolType": "stock"}})
    assert not is_equity("bybit", {"info": {"symbolType": "perp"}})
    assert is_equity("okx", {"info": {"instCategory": "3"}})
    assert is_equity("okx", {"info": {"instCategory": 3}})   # int form too
    assert not is_equity("okx", {"info": {"instCategory": "1"}})


def test_is_equity_mexc_by_trade_zone():
    # MEXC flags equities via the trade zone, not the symbol name — so both the
    # STOCK-suffixed and the brand-named perps must classify as equity.
    stock_zone = {"conceptPlate": ["mc-trade-zone-Stock", "mc-trade-zone-tradfi"]}
    assert is_equity("mexc", {"base": "AMDSTOCK", "info": stock_zone})
    assert is_equity("mexc", {"base": "TESLA", "info": stock_zone})
    # ETFs/leveraged funds/indices sit in the Stock zone too but carry 'stockindex'
    etf_zone = {"conceptPlate": ["mc-trade-zone-Stock", "mc-trade-zone-tradfi",
                                 "mc-trade-zone-ETF", "mc-trade-zone-stockindex"]}
    assert not is_equity("mexc", {"base": "TSLL", "info": etf_zone})
    index_zone = {"conceptPlate": ["mc-trade-zone-Stock", "mc-trade-zone-stockindex"]}
    assert not is_equity("mexc", {"base": "NAS100", "info": index_zone})
    # crypto Dash lives in a non-stock zone → not an equity
    assert not is_equity("mexc", {"base": "DASH",
                                  "info": {"conceptPlate": ["mc-trade-zone-privity"]}})
    assert not is_equity("mexc", {"base": "BTC", "info": {}})


def test_is_equity_hyperliquid_by_base():
    assert is_equity("hyperliquid", {"base": "XYZ-NVDA", "info": {}})
    assert not is_equity("hyperliquid", {"base": "BTC", "info": {}})


def test_canon_market_mexc_prefers_base_coin_name():
    # brand-named MEXC perps must map to the real ticker via baseCoinName
    assert canon_market("mexc", {"base": "TESLA",
                                 "info": {"baseCoinName": "TSLA"}}) == "TSLA"
    assert canon_market("mexc", {"base": "COINBASE",
                                 "info": {"baseCoinName": "COIN"}}) == "COIN"
    # STOCK-suffixed base still resolves (baseCoinName is already clean)
    assert canon_market("mexc", {"base": "AMDSTOCK",
                                 "info": {"baseCoinName": "AMD"}}) == "AMD"
    # other venues ignore baseCoinName and use the market base
    assert canon_market("hyperliquid", {"base": "XYZ-NVDA", "info": {}}) == "NVDA"


def test_is_equity_handles_missing_info_and_unknown_exchange():
    assert not is_equity("binance", {"base": "X", "info": None})
    assert not is_equity("kraken", {"base": "AMDSTOCK", "info": {}})
