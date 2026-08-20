"""
Open-interest work list for the equity-perp universe.

These stocks are NOT in the curated `instruments` table, so the collector
resolves their exchange symbols from the stock ETL's universe and stores rows
under the canonical '<TICKER>/USDT:USDT' the charts group by.  Coverage is the
WHOLE universe (Custom Report can chart any of it); the Open Interest page
trims itself down to the top N in the route.
"""

import asyncio

import pytest

from app.db.timescale import stock_symbol
from app.oi import etl as oi_etl
from app.stocks import etl as stock_etl


UNIVERSE = {
    "binance":     [("AAPL/USDT:USDT", "AAPL", 1), ("NVDA/USDT:USDT", "NVDA", 1)],
    "bybit":       [("AAPLSTOCK/USDT:USDT", "AAPL", 1), ("ZZZSTOCK/USDT:USDT", "ZZZ", 1)],
    "mexc":        [("TESLA/USDT:USDT", "TSLA", 0.01)],
    "hyperliquid": [("XYZ-AAPL/USDC:USDC", "AAPL", 1)],
    "kraken":      [("AAPL/USDT:USDT", "AAPL", 1)],   # not a configured exchange
}


@pytest.fixture
def universe(monkeypatch):
    async def fake_universe(*_args, **_kwargs):
        return UNIVERSE
    monkeypatch.setattr(stock_etl, "build_stock_universe", fake_universe)


def _work(known: set[str]) -> list[tuple[str, str, str]]:
    return asyncio.run(oi_etl._stock_work_items(known))


def test_symbol_is_the_canonical_the_charts_group_by():
    assert stock_symbol("AAPL") == "AAPL/USDT:USDT"


def test_resolves_each_exchange_symbol_for_every_ticker(universe):
    work = _work(set())
    assert ("binance", "AAPL/USDT:USDT", "AAPL/USDT:USDT") in work
    assert ("bybit", "AAPLSTOCK/USDT:USDT", "AAPL/USDT:USDT") in work
    assert ("hyperliquid", "XYZ-AAPL/USDC:USDC", "AAPL/USDT:USDT") in work
    assert ("mexc", "TESLA/USDT:USDT", "TSLA/USDT:USDT") in work


def test_covers_the_whole_universe_not_just_the_displayed_top(universe):
    canonicals = {c for _ex, _sym, c in _work(set())}
    assert canonicals == {"AAPL/USDT:USDT", "NVDA/USDT:USDT", "TSLA/USDT:USDT", "ZZZ/USDT:USDT"}


def test_skips_exchanges_that_are_not_configured(universe):
    work = _work(set())
    assert not any(ex == "kraken" for ex, _sym, _c in work)


def test_curated_instruments_win_so_oi_is_not_polled_twice(universe):
    canonicals = {c for _ex, _sym, c in _work({"AAPL/USDT:USDT"})}
    assert canonicals == {"NVDA/USDT:USDT", "TSLA/USDT:USDT", "ZZZ/USDT:USDT"}


def test_universe_failure_yields_no_work_instead_of_crashing(monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("hyperliquid 429")
    monkeypatch.setattr(stock_etl, "build_stock_universe", boom)
    assert _work(set()) == []
