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

_POLL_SEC = 300               # re-check the wall clock this often (see loop)
_MORNING_REFRESH_SEC = 1800   # inside 06:00→09:00 МСК: refresh every 30 min
_IDLE_REFRESH_SEC = 6 * 3600  # rest of the day: refresh every 6 h

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


def _refresh_due(now: datetime, last_run: datetime) -> bool:
    """Whether enough wall-clock time has elapsed to run another pass.

    30 min apart inside the 06:00→09:00 МСК deadline window, 6 h apart the rest
    of the day.  Comparing wall clocks (not a fixed sleep) is what lets the loop
    recover after a host suspend: on resume ``now`` jumps forward, the elapsed
    gap exceeds the threshold, and the next poll fires a pass immediately.
    """
    in_morning_window = _WINDOW_START_HOUR <= now.hour < _DEADLINE_HOUR
    threshold = _MORNING_REFRESH_SEC if in_morning_window else _IDLE_REFRESH_SEC
    return (now - last_run).total_seconds() >= threshold


async def spb_oi_etl_loop() -> None:
    """Background task: keep SPB open interest fresh, self-healing across host
    sleep.

    The schedule is driven by *polling the wall clock* on a short interval, not
    by one long ``asyncio.sleep`` per window.  A single multi-hour sleep does
    not survive the host (Docker Desktop VM) suspending: the event loop's
    monotonic timer is frozen while the VM is suspended, but the wall clock
    jumps forward on resume, so the sleep fires hours-to-days late and the loop
    silently stalls for days (observed 2026-07-13: OI missed a trading day).
    Re-reading ``datetime.now`` every few minutes reacts to the real wall-clock
    time and recovers on its own right after a resume.

    Cadence: every 30 min inside the 06:00→09:00 МСК window (so the previous
    day's OI lands before the 09:00 deadline whenever the exchange publishes
    it), every 6 h otherwise (catches late fills and overnight publishes).
    """
    await _safe_pass()  # immediate refresh so a fresh deploy has data at once
    last_run = datetime.now(_TZ)
    while True:
        await asyncio.sleep(_POLL_SEC)
        if _refresh_due(datetime.now(_TZ), last_run):
            await _safe_pass()
            last_run = datetime.now(_TZ)


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
