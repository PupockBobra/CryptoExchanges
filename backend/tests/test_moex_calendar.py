"""MOEX FORTS calendar semantics (moex/calendar.py + data/moex_calendar.json).

ДСВД weekend sessions: turnover IS counted (value day) but the date is NOT a
trading day (excluded from the ADTV denominator). Mixing these up skews every
MOEX ADTV figure.
"""

from datetime import date

from app.moex.calendar import (
    is_moex_trading_day,
    is_moex_value_day,
    trading_days_in_range,
    week_bounds,
)


def test_regular_weekday_counts_for_both():
    d = date(2026, 1, 12)  # Monday, no holiday
    assert is_moex_trading_day(d)
    assert is_moex_value_day(d)


def test_official_holiday_counts_for_neither():
    d = date(2026, 1, 7)  # Orthodox Christmas (Wednesday), in non_trading_days
    assert not is_moex_trading_day(d)
    assert not is_moex_value_day(d)


def test_dsvd_weekend_session_is_value_day_but_not_trading_day():
    d = date(2026, 1, 3)  # Saturday, in dsvd_new_sessions
    assert is_moex_value_day(d)
    assert not is_moex_trading_day(d)


def test_plain_weekend_counts_for_neither():
    d = date(2026, 1, 10)  # Saturday, not a ДСВД session
    assert not is_moex_trading_day(d)
    assert not is_moex_value_day(d)


def test_trading_days_in_range():
    # Jan 1–11 2026: holidays Jan 1,2,5–9; ДСВД Jan 3,4; weekend Jan 10,11 → 0
    assert trading_days_in_range(date(2026, 1, 1), date(2026, 1, 11)) == 0
    # Jan 12–16 2026: a full regular Mon–Fri week
    assert trading_days_in_range(date(2026, 1, 12), date(2026, 1, 16)) == 5


def test_week_bounds():
    monday, sunday = week_bounds(date(2026, 7, 8))  # a Wednesday
    assert monday == date(2026, 7, 6)
    assert sunday == date(2026, 7, 12)
    assert week_bounds(monday) == (monday, sunday)  # idempotent on Mondays
