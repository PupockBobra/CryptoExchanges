"""OKR baskets and the FORTS day sweep (okr/config.py, moex/fetcher.py).

The two baskets are the whole definition of the OKR numerator, and the ratio is
silently wrong — not obviously broken — if an asset lands in the wrong one or in
both.  Same for the denominator: a curated US stock listed among the ohlcv bases
would be counted twice, once there and once through the stock ETL.
"""

import datetime as dt

from app.db.timescale import US_STOCK_CURATED_BASES
from app.moex.config import ASSET_ISS_CODE
from app.moex.fetcher import fetch_market_value_by_assetcode
from app.okr.config import (
    COMMODITY_BASKET,
    FOREIGN_SECURITIES_BASKET,
    NUMERATOR_ASSETS,
    TRADFI_OHLCV_BASES,
)


def test_baskets_do_not_overlap():
    assert not set(COMMODITY_BASKET) & set(FOREIGN_SECURITIES_BASKET)


def test_baskets_have_no_duplicates():
    for basket in (COMMODITY_BASKET, FOREIGN_SECURITIES_BASKET):
        assert len(basket) == len(set(basket))


def test_numerator_is_both_baskets():
    assert set(NUMERATOR_ASSETS) == set(COMMODITY_BASKET) | set(FOREIGN_SECURITIES_BASKET)


def test_commodity_basket_covers_mains_and_minis():
    # A mini is a separate contract, so it adds to the parent rather than
    # duplicating it — both must be present or the basket understates turnover.
    for main, mini in (("GOLD", "GOLDM"), ("SILV", "SILVM"), ("BR", "BRM"), ("NG", "NGM")):
        assert main in COMMODITY_BASKET
        assert mini in COMMODITY_BASKET


def test_commodity_basket_excludes_domestic_russian_goods():
    # Wheat, petrol, diesel and the rouble-per-gram metals are not mirrors of a
    # foreign benchmark, whatever their turnover.
    for domestic in ("WHEAT", "AI92", "AI95", "DTL", "SUGAR", "GL", "SL",
                     "GLDRUBTOM", "SLVRUBTOM"):
        assert domestic not in COMMODITY_BASKET


def test_foreign_basket_excludes_moex_crypto_indices():
    # BTC/ETH/SOL/XRP/TRX on FORTS track a MOEX index, not a foreign security.
    for crypto_code in ("BTC", "ETH", "SOL", "XRP", "TRX"):
        assert crypto_code not in NUMERATOR_ASSETS
        assert crypto_code in ASSET_ISS_CODE      # they do exist on FORTS


def test_foreign_basket_excludes_russian_index_and_share_codes():
    for russian in ("MIX", "RTS", "IMOEX", "RGBI", "SBRF", "GAZR", "LKOH"):
        assert russian not in NUMERATOR_ASSETS


def test_denominator_bases_exclude_curated_us_stocks():
    # These already arrive through stock_daily_volume; counting the ohlcv rows
    # too would double them in the denominator.
    assert not set(TRADFI_OHLCV_BASES) & set(US_STOCK_CURATED_BASES)


def test_denominator_bases_are_not_crypto():
    for coin in ("BTC", "ETH", "SOL", "XRP", "TRX"):
        assert coin not in TRADFI_OHLCV_BASES


class _FakeSession:
    """Stands in for the curl_cffi session: serves canned ISS pages."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        page = self.pages[len(self.calls) - 1]
        return _FakeResponse(page)


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"history": {"columns": ["SECID", "ASSETCODE", "VALUE"], "data": self._rows}}


def _patch_session(monkeypatch, pages):
    session = _FakeSession(pages)
    monkeypatch.setattr("app.moex.fetcher._make_session", lambda: session)
    return session


def test_day_sweep_sums_all_series_of_an_asset(monkeypatch):
    # OI and turnover spread across the live series of one asset — the sweep must
    # add them up, the way the per-assetcode ETL does.
    _patch_session(monkeypatch, [[
        ["BRU6", "BR", 100.0],
        ["BRV6", "BR", 50.0],
        ["GDU6", "GOLD", 30.0],
    ]])
    totals = fetch_market_value_by_assetcode(dt.date(2026, 8, 19))
    assert totals == {"BR": 150.0, "GOLD": 30.0}


def test_day_sweep_paginates_until_a_short_page(monkeypatch):
    full = [[f"BR{i}", "BR", 1.0] for i in range(100)]
    session = _patch_session(monkeypatch, [full, [["GDU6", "GOLD", 5.0]]])
    totals = fetch_market_value_by_assetcode(dt.date(2026, 8, 19))
    assert totals == {"BR": 100.0, "GOLD": 5.0}
    assert [c["start"] for c in session.calls] == [0, 100]


def test_day_sweep_returns_empty_on_a_non_trading_day(monkeypatch):
    # ISS publishes no rows at all for weekends; the ETL reads that as "skip",
    # which is what keeps the chart from drawing a zero-ratio Saturday.
    _patch_session(monkeypatch, [[]])
    assert fetch_market_value_by_assetcode(dt.date(2026, 8, 15)) == {}


def test_day_sweep_ignores_rows_without_an_assetcode(monkeypatch):
    _patch_session(monkeypatch, [[["XXX", None, 10.0], ["BRU6", "BR", 7.0]]])
    assert fetch_market_value_by_assetcode(dt.date(2026, 8, 19)) == {"BR": 7.0}
