"""Funding derived from СПБ Биржа's own feed.

Pinned here because all three are load-bearing and silent when wrong:
the ``flag`` bit that separates a real 0 % from "not published yet", the lot
(the feed's own lot is 1.0 for crypto index perps, which would skew their
percentages by orders of magnitude), and the percentage formula itself —
verified against the channel's CSV for the same day.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.spb.funding_exchange import (
    build_funding_rows,
    funding_date,
    in_window,
    is_published,
    typical_price,
)

_MSK = ZoneInfo("Europe/Moscow")


def _rec(symbol, value, flag=0, **desc):
    d = {
        "symbol": symbol,
        "periodStart": "2026-08-19T19:00:00",
        "periodFinish": "2026-08-19T23:00:00",
        "phigh": 224.84, "plow": 223.69, "pclose": 224.01, "popen": 224.28,
    }
    d.update(desc)
    return {"fundingPerContract": {"value": value}, "flag": flag, "instrumentApiDescription": d}


def test_window_spans_midnight():
    assert in_window(datetime(2026, 8, 19, 23, 10, tzinfo=_MSK))
    assert in_window(datetime(2026, 8, 20, 0, 30, tzinfo=_MSK))
    assert in_window(datetime(2026, 8, 20, 11, 29, tzinfo=_MSK))
    assert not in_window(datetime(2026, 8, 20, 11, 31, tzinfo=_MSK))
    assert not in_window(datetime(2026, 8, 20, 16, 20, tzinfo=_MSK))   # observed: feed reads 0


def test_unpublished_zero_is_not_a_zero_rate():
    # Outside the window every record reads 0 without the flag bit; storing that
    # would paint the whole heatmap with fake zeros.
    assert not is_published(0, 0)
    assert not is_published(None, 2)
    assert is_published(0, 2)          # genuine zero, as the exchange renders it
    assert is_published(-0.0896, 0)


def test_date_is_the_period_open():
    assert funding_date({"periodStart": "2026-08-19T19:00:00"}) == date(2026, 8, 19)
    assert funding_date({}) is None


def test_typical_price_falls_back_to_close():
    assert typical_price({"phigh": 3.0, "plow": 1.0, "pclose": 2.0}) == 2.0
    assert typical_price({"priceCloseIq": 223.78}) == 223.78
    assert typical_price({}) is None


def test_percentages_match_the_channel_formula():
    """BTC 19-08-2026 from the channel CSV: fund_curr 0.00109425, MeanPrice
    68733.95 → 0.01592 % day, 5.811 % year.  Feeding that price through our
    derivation must reproduce them."""
    rows = build_funding_rows([
        _rec("BTCUSDperpA", 0.0010942499999999,
             phigh=68733.95333333, plow=68733.95333333, pclose=68733.95333333)
    ])
    (day, ticker, pct_year, pct_day, fund, price, mean_index), = rows
    assert (day, ticker) == (date(2026, 8, 19), "BTCUSDperpA")
    assert fund == 0.0010942499999999
    assert round(pct_day, 5) == 0.01592
    assert round(pct_year, 3) == 5.811
    # No index price in the feed — and it is what keeps these rows from
    # overwriting channel rows in upsert_spb_funding_from_exchange.
    assert mean_index is None


def test_skips_foreign_and_unpublished_records():
    rows = build_funding_rows([
        _rec("BTCUSDperpA", 0),                 # not published yet
        _rec("SOMEperpA", 1.23),                # not an instrument we track
        _rec("AMDperpA", -0.0896666699999999),  # good
    ])
    assert [r[1] for r in rows] == ["AMDperpA"]
