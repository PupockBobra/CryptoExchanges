"""SPB config invariants + Finam field parsing.

Regression target (CLAUDE.md): the crypto-index lot multipliers — without
them the backfill turnover is inflated thousands of times (turnover ≈
volume × typical price × lot). Lots were verified empirically against live
quotes; pin them.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from datetime import date

from app.spb.config import SPB_GROUPS, SPB_INSTRUMENTS, SPB_LOTS, SPB_NAMES, SPB_TICKERS
from app.spb.etl import _current_trading_day
from app.spb.etl import _refresh_due as _vol_refresh_due
from app.spb.fetcher import num_value
from app.spb.oi_etl import _CODE_TO_TICKER, _num, _refresh_due

_MSK = ZoneInfo("Europe/Moscow")


def test_crypto_index_lots_pinned():
    assert SPB_LOTS["BTCUSDperpA"] == 0.0001
    assert SPB_LOTS["ETHUSDperpA"] == 0.001
    assert SPB_LOTS["SOLUSDperpA"] == 0.1
    assert SPB_LOTS["XRPUSDperpA"] == 10.0
    assert SPB_LOTS["TRXUSDperpA"] == 10.0


def test_all_us_market_perps_are_lot_one():
    for ticker, (_name, lot, group) in SPB_INSTRUMENTS.items():
        if group == "US Market":
            assert lot == 1.0, f"{ticker} lot changed from 1.0"


def test_derived_lookups_cover_every_instrument():
    assert len(SPB_TICKERS) == 25
    assert set(SPB_NAMES) == set(SPB_LOTS) == set(SPB_GROUPS) == set(SPB_INSTRUMENTS)


def test_oi_code_mapping_strips_trailing_a():
    # СПБ's daily-results feed drops the trailing "A" of the Finam ticker.
    assert _CODE_TO_TICKER["BTCUSDperp"] == "BTCUSDperpA"
    assert _CODE_TO_TICKER["AMZNperp"] == "AMZNperpA"
    assert len(_CODE_TO_TICKER) == len(SPB_TICKERS)
    for code, ticker in _CODE_TO_TICKER.items():
        assert ticker == code + "A"


def test_num_value_parses_finam_nested_and_flat_fields():
    assert num_value({"volume": {"value": "1.5"}}, "volume") == 1.5
    assert num_value({"volume": "2"}, "volume") == 2.0
    assert num_value({"volume": 3}, "volume") == 3.0
    # Days with no trades omit the field entirely → 0, not a crash
    assert num_value({}, "volume") == 0.0
    assert num_value({"volume": None}, "volume") == 0.0
    assert num_value({"volume": {"value": ""}}, "volume") == 0.0
    assert num_value({"volume": "abc"}, "volume") == 0.0


def test_oi_num_helper():
    assert _num("5.5") == 5.5
    assert _num(None) == 0.0
    assert _num("x") == 0.0


def test_oi_refresh_due_idle_and_morning_cadence():
    # Idle daytime: 6 h apart, not sooner.
    noon = datetime(2026, 7, 13, 12, 0, tzinfo=_MSK)
    assert not _refresh_due(noon, noon - timedelta(hours=5))
    assert _refresh_due(noon, noon - timedelta(hours=6))

    # Inside the 06:00→09:00 МСК deadline window: 30 min apart, not sooner.
    morning = datetime(2026, 7, 13, 7, 0, tzinfo=_MSK)
    assert not _refresh_due(morning, morning - timedelta(minutes=20))
    assert _refresh_due(morning, morning - timedelta(minutes=30))


def test_volume_refresh_due_hourly_and_morning_cadence():
    # Daytime: hourly, not sooner — today's live turnover keeps growing.
    noon = datetime(2026, 7, 13, 12, 0, tzinfo=_MSK)
    assert not _vol_refresh_due(noon, noon - timedelta(minutes=50))
    assert _vol_refresh_due(noon, noon - timedelta(hours=1))

    # Inside the 06:00→09:00 МСК deadline window: 30 min apart, not sooner.
    morning = datetime(2026, 7, 13, 7, 0, tzinfo=_MSK)
    assert not _vol_refresh_due(morning, morning - timedelta(minutes=20))
    assert _vol_refresh_due(morning, morning - timedelta(minutes=30))


def test_current_trading_day_anchors_to_latest_bar_not_utc_today():
    # Regression (2026-07-22): the SPB perp trading day rolls at 04:00 UTC, so
    # in the 00:00–04:00 UTC window the live quote still reports the PREVIOUS
    # session while date.today() (UTC) has already advanced.  Stamping the quote
    # to date.today() booked ~a full previous day onto the new date (12× the real
    # partial).  The current day must follow the latest bar, not the UTC clock.
    bars = [
        {"timestamp": "2026-07-20T04:00:00Z"},
        {"timestamp": "2026-07-21T04:00:00Z"},  # latest session with a bar
    ]
    utc_today = date(2026, 7, 22)  # UTC already rolled; 07-22 session not open yet
    assert _current_trading_day(bars, utc_today) == date(2026, 7, 21)


def test_current_trading_day_falls_back_when_no_bars():
    fallback = date(2026, 7, 22)
    assert _current_trading_day([], fallback) == fallback
    assert _current_trading_day([{"timestamp": None}], fallback) == fallback


def test_oi_refresh_due_recovers_after_host_suspend():
    # Regression (2026-07-13): a multi-hour host sleep must not stall the loop.
    # last_run before the suspend, wall clock jumps ~10 h forward on resume →
    # the very next poll is due, so data reloads on its own without a restart.
    before_suspend = datetime(2026, 7, 13, 2, 0, tzinfo=_MSK)
    after_resume = datetime(2026, 7, 13, 12, 0, tzinfo=_MSK)
    assert _refresh_due(after_resume, before_suspend)
