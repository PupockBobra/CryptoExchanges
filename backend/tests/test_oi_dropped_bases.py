"""Which equity perps the Open Interest daily chart hides."""

import asyncio

import pytest

from app.api.routes import open_interest as oi
from app.stocks.config import KOREAN_TICKERS, TOP_STOCKS_DISPLAYED


@pytest.fixture
def dropped(monkeypatch):
    """Run _dropped_stock_bases against a fake DB."""
    def run(top: list[str], oi_bases: list[str]) -> set[str]:
        async def fake_top(n):
            assert n == TOP_STOCKS_DISPLAYED
            return top

        async def fake_bases():
            return oi_bases

        monkeypatch.setattr(oi, "fetch_top_stock_tickers", fake_top)
        monkeypatch.setattr(oi, "fetch_oi_equity_bases", fake_bases)
        return asyncio.run(oi._dropped_stock_bases())
    return run


def test_top_ranked_stocks_are_kept(dropped):
    """Ranked names survive; the other curated US stocks are dropped as unranked."""
    out = dropped(top=["NVDA", "TSLA"], oi_bases=["NVDA", "TSLA"])
    assert not {"NVDA", "TSLA"} & out


def test_unranked_stock_is_dropped(dropped):
    assert "MRVL" in dropped(top=["NVDA"], oi_bases=["NVDA", "MRVL"])


def test_fresh_listing_with_no_volume_history_is_dropped(dropped):
    """
    UNITREE (31.07.2026): the OI collector picks a new perp up from the live
    market scan, but the stock ETL writes no volume row until the venue closes a
    daily candle — so it cannot be in the ranking yet, and must not render.
    """
    assert "UNITREE" in dropped(top=["NVDA", "TSLA"], oi_bases=["NVDA", "TSLA", "UNITREE"])


def test_korean_names_are_never_dropped(dropped):
    """They have their own section and never compete for a US Market slot."""
    out = dropped(top=["NVDA"], oi_bases=["NVDA", *KOREAN_TICKERS])
    assert not out & set(KOREAN_TICKERS)


def test_curated_us_stocks_are_dropped_when_unranked(dropped):
    """
    They live in `instruments`, so the equity query never returns them — they
    have to be added explicitly or an unranked AAPL would keep its card.
    """
    assert {"AAPL", "MSFT"} <= dropped(top=["NVDA"], oi_bases=["NVDA"])


def test_crypto_and_commodities_are_untouched(dropped):
    """Only equity bases reach the drop list — curated symbols are filtered in SQL."""
    out = dropped(top=["NVDA"], oi_bases=["NVDA"])
    assert not {"BTC", "ETH", "XAU", "BRN"} & out
