"""Top-N crypto-perp universe: what counts as crypto, and how it is ranked.

A leak here silently double-counts an asset the other slices already own — an
equity or commodity perp landing in the Cryptocurrencies group is exactly the bug
this ETL was built to fix (Korean stocks used to sit in that slice).
"""

from app.crypto.config import base_of, is_crypto_perp, rank_top, turnover_24h


def perp(base: str, quote: str = "USDT", **info) -> dict:
    return {
        "swap": True, "type": "swap", "base": base, "quote": quote,
        "contractSize": 1, "info": info,
    }


class TestIsCryptoPerp:
    def test_plain_altcoin_perp_is_crypto(self):
        assert is_crypto_perp("binance", perp("DOGE"))

    def test_spot_market_is_not_a_perp(self):
        spot = perp("DOGE")
        spot["swap"], spot["type"] = False, "spot"
        assert not is_crypto_perp("binance", spot)

    def test_exotic_quote_is_rejected(self):
        """Coin-margined contracts report turnover in a non-USD unit."""
        assert not is_crypto_perp("binance", perp("BTC", quote="BTC"))

    def test_equity_perp_is_rejected_by_the_venue_flag(self):
        assert not is_crypto_perp("binance", perp("AAPL", underlyingType="EQUITY"))

    def test_commodity_and_index_tickers_are_rejected(self):
        assert not is_crypto_perp("binance", perp("XAU"))
        assert not is_crypto_perp("okx", perp("QQQ"))
        assert not is_crypto_perp("mexc", perp("NATGAS"))

    def test_korean_stock_ticker_is_rejected(self):
        """The bug that started this: SKHYNIX counted as a cryptocurrency."""
        assert not is_crypto_perp("bybit", perp("SKHYNIX"))

    def test_builder_dex_namespace_is_rejected(self):
        """Hyperliquid's XYZ-/CASH- listings are real-world assets."""
        assert not is_crypto_perp("hyperliquid", perp("XYZ-GOLD"))
        assert not is_crypto_perp("hyperliquid", perp("CASH-NVDA"))
        assert not is_crypto_perp("hyperliquid", perp("XYZ-SOMETHINGNEW"))

    def test_base_of_strips_the_namespace(self):
        assert base_of(perp("XYZ-CL")) == "CL"
        assert base_of(perp("DOGE")) == "DOGE"


class TestTurnover24h:
    def test_quote_volume_wins_when_present(self):
        assert turnover_24h({"quoteVolume": 100, "baseVolume": 2, "last": 3}) == 100

    def test_falls_back_to_base_times_last(self):
        """Venues that leave quoteVolume empty would otherwise rank as zero."""
        assert turnover_24h({"baseVolume": 2, "last": 3}) == 6

    def test_missing_ticker_is_zero(self):
        assert turnover_24h({}) == 0.0
        assert turnover_24h(None) == 0.0


class TestRankTop:
    markets = {
        "DOGE/USDT:USDT": perp("DOGE"),
        "BTC/USDT:USDT":  perp("BTC"),
        "PEPE/USDT:USDT": perp("PEPE"),
        "AAPL/USDT:USDT": perp("AAPL", underlyingType="EQUITY"),
        "XAU/USDT:USDT":  perp("XAU"),
    }
    tickers = {
        "DOGE/USDT:USDT": {"quoteVolume": 50},
        "BTC/USDT:USDT":  {"quoteVolume": 900},
        "PEPE/USDT:USDT": {"quoteVolume": 10},
        "AAPL/USDT:USDT": {"quoteVolume": 999},
        "XAU/USDT:USDT":  {"quoteVolume": 999},
    }

    def test_ranks_by_turnover_and_drops_non_crypto(self):
        top = rank_top(self.markets, self.tickers, "binance", top_n=10)
        assert [base for _s, base, _cs in top] == ["BTC", "DOGE", "PEPE"]

    def test_caps_at_top_n(self):
        top = rank_top(self.markets, self.tickers, "binance", top_n=2)
        assert [base for _s, base, _cs in top] == ["BTC", "DOGE"]

    def test_contract_size_is_carried_for_mexc_only(self):
        markets = {"DOGE/USDT:USDT": {**perp("DOGE"), "contractSize": 0.01}}
        tickers = {"DOGE/USDT:USDT": {"quoteVolume": 1}}
        assert rank_top(markets, tickers, "mexc")[0][2] == 0.01
        assert rank_top(markets, tickers, "binance")[0][2] == 1.0
