"""
MOEX FORTS open-interest ETL.

MOEX has no exchange API for us to poll, but ISS publishes OPENPOSITION and
OPENPOSITIONVALUE per series and trading day.  For each tracked asset we sum
both across all its series (an asset's OI lives in several live contracts) and
store the result in the shared ``open_interest`` table as exchange='moex', under
the same canonical symbol the volume charts use — so the Open Interest page and
Custom Report pick it up with no extra plumbing.

Roubles → USD: ``open_interest.oi_usdt`` is USD by contract for every source, and
the queries multiply by USDRUBF on the way out.  ISS gives roubles, so we divide
by the same forward-filled rate here; the round trip cancels out and the page
shows exactly what ISS published.

Runs at startup and every 6 h, like the turnover ETL.  ISS fetches are blocking
(curl_cffi), so they run in a thread-pool executor.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

from app.db.timescale import (
    fetch_moex_fx_rates_range,
    get_moex_oi_latest_date,
    upsert_open_interest,
)
from app.moex.config import ASSET_TO_CANONICAL, OI_EXCLUDED_ASSETS, iss_codes_for
from app.moex.fetcher import aggregate_asset_oi_by_assetcode

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 7            # re-fetch recent days (ISS posts late corrections)
_INITIAL_LOOKBACK_DAYS = 180  # first run on an empty table
_POLL_INTERVAL = 21_600       # 6 h

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="moex-oi")


async def moex_oi_etl_loop() -> None:
    """Background task: run once at startup then every 6 h."""
    while True:
        try:
            await run_moex_oi_etl()
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.warning("MOEX OI ETL: pass failed: %s", exc)
        await asyncio.sleep(_POLL_INTERVAL)


async def run_moex_oi_etl() -> None:
    """One full pass over the tracked FORTS assets."""
    log.info("MOEX OI ETL: starting pass")
    latest  = await get_moex_oi_latest_date()
    today   = date.today()
    from_dt = (
        today - timedelta(days=_INITIAL_LOOKBACK_DAYS) if latest is None
        else latest - timedelta(days=_LOOKBACK_DAYS)
    )

    fx = {r["date"]: float(r["usdrub"]) for r in await fetch_moex_fx_rates_range(from_dt, today)}
    if not fx:
        log.warning("MOEX OI ETL: no USDRUBF rates in range — skipping pass")
        return

    loop = asyncio.get_event_loop()
    stored = 0
    for asset_code, canonical in ASSET_TO_CANONICAL.items():
        if asset_code in OI_EXCLUDED_ASSETS:
            continue
        # Mini contracts (BRM/NGM/GOLDM/SILVM) are summed into the parent asset.
        totals: dict[date, tuple[float, float]] = {}
        try:
            for iss_code in iss_codes_for(asset_code):
                part = await loop.run_in_executor(
                    _executor, aggregate_asset_oi_by_assetcode, iss_code, from_dt, today
                )
                for d, (contracts, value_rub) in part.items():
                    prev_c, prev_v = totals.get(d, (0.0, 0.0))
                    totals[d] = (prev_c + contracts, prev_v + value_rub)
        except Exception as exc:  # noqa: BLE001 — one bad asset must not kill the pass
            log.warning("MOEX OI ETL: asset %s failed: %s", asset_code, exc)
            continue

        rows: list[tuple] = []
        for d, (contracts, value_rub) in totals.items():
            rate = fx.get(d)
            if not rate or value_rub <= 0:
                continue
            ts = datetime.combine(d, time(0, 0), tzinfo=timezone.utc)
            rows.append((ts, "moex", canonical, contracts, value_rub / rate))
        stored += await upsert_open_interest(rows)

    log.info("MOEX OI ETL: pass complete — upserted %d rows", stored)


async def run_moex_oi_etl_safe() -> None:
    """Wrapper used by the manual refresh endpoint."""
    try:
        await run_moex_oi_etl()
    except Exception as exc:  # noqa: BLE001
        log.warning("MOEX OI ETL: manual refresh failed: %s", exc)
