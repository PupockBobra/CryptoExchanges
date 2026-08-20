"""
SPB Exchange perpetual-futures ETL job.

Runs at startup and every 6 h.  For each ticker:
  1. Fetch daily bars since the last stored date (or 180 d back on first run).
  2. Approximate per-day turnover (USD) = volume × typical price (H+L+C)/3 —
     Finam's daily bars expose volume only, not turnover.
  3. Overwrite *today's* row with the exact ``turnover`` from the live quote
     (the only place the API reports real money turnover).  Over time the table
     fills with exact values captured live; only the initial backfill stays
     approximate.

USD→RUB conversion happens at query time via the shared ``moex_fx_rates`` table,
so we store turnover in USD here.

The job is a no-op when ``FINAM_API_TOKEN`` is unset, and every error is caught
so the loop never crashes the backend.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta

from app.config import settings
from app.db.timescale import get_spb_latest_date, upsert_spb_daily_volume
from app.spb.config import SPB_LOTS, SPB_TICKERS
from app.spb.fetcher import FinamClient, num_value

log = logging.getLogger(__name__)

# Re-fetch this many days back even when the DB has recent data (catches late fills).
_LOOKBACK_DAYS = 7
# How far back to fetch on the very first run (empty table).
_INITIAL_LOOKBACK_DAYS = 180
# Gentle pacing between Finam calls — the API resets rapid sequential requests.
_THROTTLE_SEC = 0.4


async def spb_etl_loop() -> None:
    """Background task: run the ETL once at startup then every 6 h."""
    while True:
        try:
            await run_spb_etl()
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.warning("SPB ETL: loop iteration failed: %s", exc)
        await asyncio.sleep(21_600)


async def run_spb_etl() -> None:
    """One full ETL pass.  Errors are caught so the loop never crashes."""
    if not settings.finam_api_token:
        log.info("SPB ETL: FINAM_API_TOKEN not set — skipping")
        return

    log.info("SPB ETL: starting pass")
    try:
        async with FinamClient(settings.finam_api_token) as client:
            for ticker in SPB_TICKERS:
                try:
                    await _refresh_ticker(client, ticker)
                except Exception as exc:
                    log.warning("SPB ETL: ticker %s failed: %s", ticker, exc)
                await asyncio.sleep(_THROTTLE_SEC)
    except Exception as exc:
        log.warning("SPB ETL: pass failed: %s", exc)
    log.info("SPB ETL: pass complete")


def _from_date(latest) -> date:
    today = date.today()
    if latest is None:
        return today - timedelta(days=_INITIAL_LOOKBACK_DAYS)
    return latest - timedelta(days=_LOOKBACK_DAYS)


async def _refresh_ticker(client: FinamClient, ticker: str) -> None:
    latest = await get_spb_latest_date(ticker)
    from_dt = _from_date(latest)
    today = date.today()

    lot = SPB_LOTS.get(ticker, 1.0)
    bars = await client.fetch_daily_bars(ticker, from_dt, today)

    # Historical days: approximate turnover from the daily bar.
    # turnover ≈ volume × typical price × lot  (lot ≠ 1 for the crypto-index perps).
    rows: dict[date, tuple] = {}
    for b in bars:
        ts = b.get("timestamp")
        if not ts:
            continue
        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        vol = num_value(b, "volume")
        high, low, close = num_value(b, "high"), num_value(b, "low"), num_value(b, "close")
        if vol <= 0 or close <= 0:
            continue
        typical = (high + low + close) / 3 if high > 0 and low > 0 else close
        rows[d] = (d, ticker, round(vol, 2), round(vol * typical * lot, 2))

    # Current day: overwrite with the exact live turnover, if the quote reports any.
    await asyncio.sleep(_THROTTLE_SEC)
    quote = await client.fetch_latest_quote(ticker)
    q_turn = num_value(quote, "turnover")
    if q_turn > 0:
        rows[today] = (today, ticker, round(num_value(quote, "volume"), 2), round(q_turn, 2))

    n = await upsert_spb_daily_volume(list(rows.values()))
    log.info("SPB ETL: %s upserted %d rows (from %s)", ticker, n, from_dt)
