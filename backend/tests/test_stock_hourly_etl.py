"""Hourly equity-perp ETL: fetch window and bar folding.

Both halves have bitten the daily backfills before — MEXC placeholder bars dated
into the future, and a `since` that reaches past the retention window and writes
rows the retention job deletes right back.
"""

from datetime import datetime, timedelta, timezone

from app.stocks.hourly_etl import (
    BACKFILL_DAYS,
    LOOKBACK_HOURS,
    accumulate_bars,
    since_ms_for,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
MS = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731


class TestSinceMsFor:
    def test_empty_table_starts_at_the_backfill_window(self):
        assert since_ms_for(None, NOW) == MS(NOW - timedelta(days=BACKFILL_DAYS))

    def test_incremental_run_looks_back_a_few_hours(self):
        latest = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        assert since_ms_for(latest, NOW) == MS(latest - timedelta(hours=LOOKBACK_HOURS))

    def test_never_reaches_past_the_backfill_window(self):
        stale = NOW - timedelta(days=400)
        assert since_ms_for(stale, NOW) == MS(NOW - timedelta(days=BACKFILL_DAYS))

    def test_naive_timestamp_is_treated_as_utc(self):
        naive = datetime(2026, 8, 3, 10, 0)
        assert since_ms_for(naive, NOW) == MS(
            datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        )


class TestAccumulateBars:
    since = MS(datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc))
    now = MS(NOW)

    def _fold(self, bars, contract_size=1.0, ticker="AAPL"):
        out: dict = {}
        accumulate_bars(bars, "mexc", ticker, contract_size, self.since, self.now, out)
        return out

    def test_turnover_is_close_times_volume_times_contract_size(self):
        ts = MS(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc))
        out = self._fold([[ts, 0, 0, 0, 200.0, 50.0]], contract_size=0.01)
        hour = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
        assert out == {(hour, "mexc", "AAPL"): 100.0}

    def test_bars_dated_into_the_future_are_dropped(self):
        """MEXC returns placeholder candles years ahead for some symbols."""
        future = MS(datetime(2063, 1, 1, tzinfo=timezone.utc))
        assert self._fold([[future, 0, 0, 0, 200.0, 50.0]]) == {}

    def test_bars_before_the_requested_window_are_dropped(self):
        old = MS(datetime(2026, 8, 2, 23, 0, tzinfo=timezone.utc))
        assert self._fold([[old, 0, 0, 0, 200.0, 50.0]]) == {}

    def test_zero_volume_and_missing_close_are_skipped(self):
        ts = MS(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc))
        assert self._fold([[ts, 0, 0, 0, 200.0, 0.0], [ts, 0, 0, 0, None, 5.0]]) == {}

    def test_pages_overlapping_on_an_hour_add_up(self):
        """Pagination re-reads the boundary bar, so a repeated page must not be
        silently ignored — the ETL upserts the accumulated value per hour."""
        ts = MS(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc))
        out = self._fold([[ts, 0, 0, 0, 100.0, 1.0], [ts, 0, 0, 0, 100.0, 1.0]])
        assert out == {(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc), "mexc", "AAPL"): 200.0}

    def test_bars_are_bucketed_by_the_hour_they_open(self):
        ts = MS(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc))
        ts2 = MS(datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))
        out = self._fold([[ts, 0, 0, 0, 10.0, 1.0], [ts2, 0, 0, 0, 20.0, 1.0]])
        assert sorted(k[0].hour for k in out) == [9, 10]
