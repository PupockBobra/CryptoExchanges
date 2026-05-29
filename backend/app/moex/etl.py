"""
MOEX FORTS ETL job.

Runs at startup and every 24 h.  For each pass:
  1. Refresh USDRUBF daily rates → moex_fx_rates   (forward-filled for weekends)
  2. Refresh per-asset daily VALUE sums → moex_daily_value
     (fetches all active SECID series, sums VALUE by date)

ISS fetches are blocking (curl_cffi is synchronous); they run in a thread-pool
executor so the event loop is never blocked.

Connectivity note:
  From a non-Russian IP the ISS TLS handshake is geo-blocked.
  Set MOEX_ISS_PROXY=socks5://... to route through a Russian proxy.
  Without access the ETL logs a warning and skips — the app continues normally.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from app.db.timescale import (
    get_moex_fx_latest_date,
    get_moex_asset_latest_date,
    upsert_moex_fx_rates,
    upsert_moex_daily_value,
)
from app.moex.calendar import is_moex_value_day
from app.moex.config import ASSET_TO_CANONICAL
from app.moex.fetcher import fetch_usdrubf_history, aggregate_asset_value_by_assetcode

log = logging.getLogger(__name__)

# Re-fetch this many days back even when DB has recent data (catches late corrections).
_LOOKBACK_DAYS = 7
# How far back to fetch on the very first run (empty DB).
_INITIAL_LOOKBACK_DAYS = 180

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="moex-etl")


# ── Public entry points ───────────────────────────────────────────────────────

async def moex_etl_loop() -> None:
    """Background task: run ETL once at startup then every 24 h."""
    while True:
        await run_moex_etl()
        await asyncio.sleep(86_400)


async def run_moex_etl() -> None:
    """One full ETL pass.  Errors are caught so the loop never crashes."""
    log.info("MOEX ETL: starting pass")
    try:
        await _refresh_fx_rates()
    except Exception as exc:
        log.warning("MOEX ETL: FX rates refresh failed: %s", exc)

    for asset_code in ASSET_TO_CANONICAL:
        try:
            await _refresh_asset(asset_code)
        except Exception as exc:
            log.warning("MOEX ETL: asset %s refresh failed: %s", asset_code, exc)

    log.info("MOEX ETL: pass complete")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _from_date(latest_date, initial_days: int = _INITIAL_LOOKBACK_DAYS) -> date:
    """Determine the start date for incremental fetching."""
    today = date.today()
    if latest_date is None:
        return today - timedelta(days=initial_days)
    return max(latest_date - timedelta(days=_LOOKBACK_DAYS), date(today.year, 1, 1))


async def _refresh_fx_rates() -> None:
    """Fetch USDRUBF history and forward-fill gaps into moex_fx_rates."""
    loop = asyncio.get_event_loop()
    latest = await get_moex_fx_latest_date()
    from_dt = _from_date(latest)
    today   = date.today()

    log.info("MOEX ETL: fetching USDRUBF from %s to %s", from_dt, today)
    raw_rows: list[dict] = await loop.run_in_executor(
        _executor, fetch_usdrubf_history, from_dt, today
    )

    # Build a raw rate map: date → rate
    raw: dict[date, float] = {}
    for r in raw_rows:
        d = date.fromisoformat(r["TRADEDATE"])
        rate = r.get("WAPRICE") or r.get("CLOSE")
        if rate:
            raw[d] = float(rate)

    if not raw:
        log.warning("MOEX ETL: USDRUBF returned no rows — ISS may be unreachable")
        return

    # Forward-fill every calendar day in [from_dt, today]
    filled: list[tuple] = []
    last_rate: float | None = None
    d = from_dt
    while d <= today:
        if d in raw:
            last_rate = raw[d]
        if last_rate is not None:
            filled.append((d, last_rate))
        d += timedelta(days=1)

    n = await upsert_moex_fx_rates(filled)
    log.info("MOEX ETL: upserted %d FX rate rows", n)


async def _refresh_asset(asset_code: str) -> None:
    """Fetch and aggregate VALUE for all series of one asset via ISS assetcode filter."""
    loop = asyncio.get_event_loop()
    latest = await get_moex_asset_latest_date(asset_code)
    from_dt = _from_date(latest)
    today   = date.today()

    log.info("MOEX ETL: fetching %s (all series) from %s to %s", asset_code, from_dt, today)
    totals: dict[date, float] = await loop.run_in_executor(
        _executor, aggregate_asset_value_by_assetcode, asset_code, from_dt, today
    )

    if not totals:
        log.warning("MOEX ETL: %s returned no VALUE rows", asset_code)
        return

    # Keep only dates that count as MOEX value days (incl. ДСВД, excl. holidays)
    rows = [
        (d, asset_code, round(v, 2))
        for d, v in totals.items()
        if is_moex_value_day(d) and v > 0
    ]
    n = await upsert_moex_daily_value(rows)
    log.info("MOEX ETL: upserted %d rows for %s", n, asset_code)
