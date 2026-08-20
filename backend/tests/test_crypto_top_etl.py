"""Top-N crypto ETL: fetch windows.

The daily helper receives a `date` (MAX(date)) and the hourly one a `datetime`
(MAX(hour)) straight from asyncpg — mixing those up raises at runtime, inside a
background loop where it would only show up as a silently stale chart.
"""

from datetime import date, datetime, timedelta, timezone

from app.crypto.etl import (
    DAILY_LOOKBACK_DAYS,
    HOURLY_BACKFILL_DAYS,
    HOURLY_LOOKBACK_HOURS,
    daily_since_ms,
    hourly_since_ms,
)
from app.crypto.config import BACKFILL_SINCE

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
MS = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731


class TestDailySince:
    def test_empty_venue_starts_at_the_ytd_floor(self):
        floor = datetime.combine(date.fromisoformat(BACKFILL_SINCE),
                                 datetime.min.time(), tzinfo=timezone.utc)
        assert daily_since_ms(None, NOW) == MS(floor)

    def test_incremental_run_refetches_the_last_days(self):
        assert daily_since_ms(date(2026, 8, 2), NOW) == MS(
            datetime(2026, 8, 2, tzinfo=timezone.utc) - timedelta(days=DAILY_LOOKBACK_DAYS)
        )

    def test_never_reaches_before_the_floor(self):
        assert daily_since_ms(date(2026, 1, 1), NOW) == daily_since_ms(None, NOW)


class TestHourlySince:
    def test_empty_venue_starts_at_the_retention_window(self):
        assert hourly_since_ms(None, NOW) == MS(NOW - timedelta(days=HOURLY_BACKFILL_DAYS))

    def test_incremental_run_looks_back_a_few_hours(self):
        latest = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        assert hourly_since_ms(latest, NOW) == MS(latest - timedelta(hours=HOURLY_LOOKBACK_HOURS))

    def test_stale_row_cannot_drag_the_fetch_past_retention(self):
        assert hourly_since_ms(NOW - timedelta(days=400), NOW) == MS(
            NOW - timedelta(days=HOURLY_BACKFILL_DAYS)
        )

    def test_naive_timestamp_is_treated_as_utc(self):
        assert hourly_since_ms(datetime(2026, 8, 3, 10, 0), NOW) == MS(
            datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
            - timedelta(hours=HOURLY_LOOKBACK_HOURS)
        )
