"""
SPB Exchange open-interest ETL job.

Pulls end-of-day open interest for the 25 tracked perps straight from СПБ Биржа's
own public API (no Finam token).  One HTTP request per calendar day returns every
instrument, so the job iterates dates:

  1. Start from the last stored date − 7 d (or 180 d back on first run).
  2. For each day, fetch the session=3 (end-of-day) rows and keep the ones whose
     futuresCode maps to a tracked ticker (config ticker == futuresCode + "A").
  3. Upsert (date, ticker, oi_contracts, oi_usd).  USD→RUB happens at query time.

Scheduling: the exchange publishes the previous day's end-of-day OI overnight /
early morning (МСК).  To guarantee fresh data well before 09:00 МСК, the loop
runs once at startup, then every morning refreshes repeatedly through a 06:00→
09:00 МСК window (every 30 min) — so whenever the exchange publishes, it lands
before the 09:00 deadline — plus one daytime pass for late fills.  Every error is
caught so the loop never crashes the backend.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.timescale import get_spb_oi_latest_date, upsert_spb_open_interest
from app.spb.config import SPB_TICKERS
from app.spb.spb_api import SpbApiClient

log = logging.getLogger(__name__)

# СПБ Биржа operates on Moscow time; schedule against it regardless of the
# container's (UTC) clock.
_TZ = ZoneInfo("Europe/Moscow")
_WINDOW_START_HOUR = 6   # begin the morning refresh window at 06:00 МСК
_DEADLINE_HOUR = 9       # data must be fresh by 09:00 МСК
_DAYTIME_PASS_HOUR = 14  # one afternoon pass catches results published after 09:00
_WINDOW_RETRY_SEC = 1800  # refresh every 30 min inside the window

_LOOKBACK_DAYS = 7            # re-fetch this far back to catch late fills
_INITIAL_LOOKBACK_DAYS = 180  # first-run backfill window
_THROTTLE_SEC = 0.4          # gentle pacing between per-day requests

# The API reports one-sided open interest (totalOpenPosition), but СПБ Биржа's
# own site table publishes total positions across both sides (long + short), i.e.
# exactly 2×. Store the both-sides figure so the app matches the exchange site.
_SIDES = 2

# futuresCode → config ticker.  СПБ's daily-results feed drops the trailing "A"
# (BTCUSDperpA in config → BTCUSDperp in the API).
_CODE_TO_TICKER = {t[:-1]: t for t in SPB_TICKERS}


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _safe_pass() -> None:
    try:
        await run_spb_oi_etl()
    except Exception as exc:  # noqa: BLE001 — the loop must never die
        log.warning("SPB OI ETL: pass failed: %s", exc)


async def _sleep_until_hour(hour: int) -> None:
    """Sleep until the next occurrence of ``hour``:00 МСК."""
    now = datetime.now(_TZ)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


async def spb_oi_etl_loop() -> None:
    """Background task: refresh on startup, every morning before 09:00 МСК,
    plus one afternoon pass for results the exchange publishes late."""
    await _safe_pass()  # immediate refresh so a fresh deploy has data at once
    while True:
        await _sleep_until_hour(_WINDOW_START_HOUR)
        # Refresh repeatedly through 06:00→09:00 МСК so the previous day's OI is
        # loaded well before 09:00 whenever the exchange publishes it.
        while datetime.now(_TZ).hour < _DEADLINE_HOUR:
            await _safe_pass()
            await asyncio.sleep(_WINDOW_RETRY_SEC)
        # The morning window ends at 09:00, but the exchange occasionally
        # publishes end-of-day results later — pick those up the same day
        # instead of waiting for tomorrow's window.
        await _sleep_until_hour(_DAYTIME_PASS_HOUR)
        await _safe_pass()


async def run_spb_oi_etl() -> None:
    """One full pass over the missing date range.  Errors are caught per day."""
    latest = await get_spb_oi_latest_date()
    today = date.today()
    start = (
        today - timedelta(days=_INITIAL_LOOKBACK_DAYS)
        if latest is None
        else latest - timedelta(days=_LOOKBACK_DAYS)
    )

    log.info("SPB OI ETL: starting pass from %s to %s", start, today)
    total = 0
    async with SpbApiClient() as api:
        day = start
        while day <= today:
            try:
                total += await _ingest_day(api, day)
            except Exception as exc:  # noqa: BLE001 — one bad day must not abort the run
                log.warning("SPB OI ETL: day %s failed: %s", day, exc)
            await asyncio.sleep(_THROTTLE_SEC)
            day += timedelta(days=1)
    log.info("SPB OI ETL: pass complete, upserted %d rows", total)


async def _ingest_day(api: SpbApiClient, day: date) -> int:
    rows = await api.fetch_futures_day_eod(day)

    out: list[tuple] = []
    for r in rows:
        ticker = _CODE_TO_TICKER.get(r.get("futuresCode"))
        if ticker is None:
            continue
        oi_contracts = _num(r.get("totalOpenPosition")) * _SIDES
        oi_usd = _num(r.get("totalOpenPositionVolume")) * _SIDES
        if oi_contracts <= 0 and oi_usd <= 0:
            continue  # no open interest reported for this instrument that day
        out.append((day, ticker, round(oi_contracts, 2), round(oi_usd, 2)))

    return await upsert_spb_open_interest(out)
