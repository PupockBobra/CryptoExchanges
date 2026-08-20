"""
OKR ETL — daily FORTS turnover per ISS ASSETCODE.

One market-level ISS sweep per day covers every contract (~830 SECIDs in ~9
pages of 100), so the cost is fixed no matter how wide the baskets are.  The
whole market is stored, not just the baskets: the ratio's composition is then a
config decision that needs no re-backfill.

Days are walked NEWEST FIRST, so the page has a usable tail within a minute of
the first pass while the 90-day backfill (~1 hour of ISS round-trips) fills in
behind it.

ISS fetches are blocking (curl_cffi is synchronous) and run in a thread-pool
executor.  From a non-Russian IP the ISS handshake is geo-blocked — set
MOEX_ISS_PROXY, same as the MOEX ETL; without access the pass logs and skips.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from app.db.timescale import get_okr_stored_dates, upsert_okr_moex_daily
from app.moex.fetcher import fetch_market_value_by_assetcode
from app.okr.config import BACKFILL_DAYS, ETL_INTERVAL_SEC, LOOKBACK_DAYS

log = logging.getLogger(__name__)

# Wait for the boot crowd (backfills, universe scans) to settle first.
STARTUP_DELAY_SEC = 120
# Pause between day sweeps — ISS throttles bursts.
_DAY_PAUSE_SEC = 0.5

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="okr-etl")


async def okr_etl_loop() -> None:
    """Background task: sweep once shortly after startup, then every 6 h."""
    await asyncio.sleep(STARTUP_DELAY_SEC)
    while True:
        await run_okr_etl_safe()
        await asyncio.sleep(ETL_INTERVAL_SEC)


async def run_okr_etl_safe() -> None:
    """One pass, with errors contained so the loop never dies."""
    try:
        await run_okr_etl()
    except Exception as exc:  # noqa: BLE001
        log.warning("OKR ETL: pass failed: %s", exc)


async def run_okr_etl() -> None:
    """Fetch every FORTS asset's daily turnover for the missing days."""
    loop     = asyncio.get_event_loop()
    today    = date.today()
    since    = today - timedelta(days=BACKFILL_DAYS)
    # Days newer than this are re-fetched even when stored: ISS publishes late
    # corrections, and the most recent day may have been swept mid-session.
    recheck  = today - timedelta(days=LOOKBACK_DAYS)
    stored   = await get_okr_stored_dates(since)

    days = [
        d for d in (today - timedelta(days=i) for i in range(1, BACKFILL_DAYS + 1))
        if d >= recheck or d not in stored
    ]
    if not days:
        log.info("OKR ETL: nothing to fetch")
        return

    log.info("OKR ETL: sweeping %d day(s), %s … %s", len(days), days[-1], days[0])
    written = skipped = 0
    for d in days:
        try:
            totals: dict[str, float] = await loop.run_in_executor(
                _executor, fetch_market_value_by_assetcode, d
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("OKR ETL: %s failed: %s", d, exc)
            continue
        if not totals:
            skipped += 1          # weekend / holiday — ISS publishes no rows
            continue
        rows = [(d, code, round(v, 2)) for code, v in totals.items() if v > 0]
        written += await upsert_okr_moex_daily(rows)
        await asyncio.sleep(_DAY_PAUSE_SEC)

    log.info("OKR ETL: upserted %d rows (%d non-trading days skipped)", written, skipped)
