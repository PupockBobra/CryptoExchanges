"""
MOEX trading calendar helpers.

Trading day definition (MOEX FORTS):
  - Mon–Fri
  - NOT in non_trading_days (official holidays)
  - NOT in dsvd_new_sessions (weekend sessions — turnover counted, day is NOT)

ДСВД dates: turnover IS included in volume sums but the date is NOT counted
in the ADTV denominator.
"""

import json
import logging
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_CALENDAR_PATH = Path(__file__).parent.parent / "data" / "moex_calendar.json"


@lru_cache(maxsize=1)
def _load_calendar() -> tuple[frozenset[date], frozenset[date]]:
    with open(_CALENDAR_PATH) as f:
        raw = json.load(f)
    non_trading = frozenset(date.fromisoformat(d) for d in raw.get("non_trading_days", []))
    dsvd        = frozenset(date.fromisoformat(d) for d in raw.get("dsvd_new_sessions", []))
    return non_trading, dsvd


def is_moex_trading_day(d: date) -> bool:
    """
    True if d is a standard MOEX trading day (counts in ADTV denominator).
    Mon–Fri, not a holiday, not a ДСВД date.
    """
    non_trading, dsvd = _load_calendar()
    return d.weekday() < 5 and d not in non_trading and d not in dsvd


def is_moex_value_day(d: date) -> bool:
    """
    True if MOEX turnover should be counted on d (trading day OR ДСВД session).
    Used to filter rows fetched from ISS before summing VALUE.
    """
    non_trading, dsvd = _load_calendar()
    if d in non_trading:
        return False
    if d in dsvd:
        return True                       # ДСВД: value counts, but day doesn't
    return d.weekday() < 5


def trading_days_in_range(from_date: date, till_date: date) -> int:
    """Count MOEX trading days (denominator for ADTV) in [from_date, till_date]."""
    count = 0
    d = from_date
    while d <= till_date:
        if is_moex_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def week_bounds(week_start: date) -> tuple[date, date]:
    """Return (monday, sunday) for the ISO week containing week_start."""
    monday = week_start - timedelta(days=week_start.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday
