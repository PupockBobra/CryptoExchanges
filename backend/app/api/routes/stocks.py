"""
REST API for equity (stock) perpetual-futures turnover on crypto exchanges.

GET  /api/stocks/volume?period=daily|weekly|hourly
        → { "by_exchange": [...], "by_instrument": [...] }
          daily  = last 30 days (bucket = date)
          weekly = ISO weeks since 2026-01-01 (bucket = Monday)
          hourly = mean turnover per MSK hour-of-day over the last 30 days
                   (bucket = '00'…'23', so it sorts as a plain string like the
                   date buckets do)
        Each row: { bucket, bucket_label, series, volume_rub }  (RUB).
POST /api/stocks/refresh   → trigger an equity-perp ETL pass in the background.
"""

import logging

from fastapi import APIRouter, Query, BackgroundTasks

from app.db.timescale import (
    fetch_stock_daily_volume,
    fetch_stock_weekly_volume,
    fetch_stock_hourly_profile,
)
from app.api.cache import ttl_cache

log = logging.getLogger(__name__)
router = APIRouter()


def _rows(records) -> list[dict]:
    return [
        {
            "bucket":       str(r["bucket"]),
            "bucket_label": r["bucket_label"].strip(),
            "series":       r["series"],
            "volume_rub":   float(r["volume_rub"] or 0),
        }
        for r in records
    ]


HOURLY_PROFILE_DAYS = 30


def _hour_rows(records) -> list[dict]:
    """Profile rows in the same shape as the date-bucketed ones ('13' → '13:00')."""
    return [
        {
            "bucket":       f"{int(r['hour_msk']):02d}",
            "bucket_label": f"{int(r['hour_msk']):02d}:00",
            "series":       r["series"],
            "volume_rub":   float(r["volume_rub"] or 0),
        }
        for r in records
    ]


@router.get("/volume")
@ttl_cache()
async def get_stock_volume(
    period: str = Query("weekly", pattern="^(daily|weekly|hourly)$"),
):
    if period == "hourly":
        by_exchange   = await fetch_stock_hourly_profile("exchange", HOURLY_PROFILE_DAYS)
        by_instrument = await fetch_stock_hourly_profile("instrument", HOURLY_PROFILE_DAYS)
        return {
            "period":        period,
            "by_exchange":   _hour_rows(by_exchange),
            "by_instrument": _hour_rows(by_instrument),
        }
    fetch = fetch_stock_weekly_volume if period == "weekly" else fetch_stock_daily_volume
    by_exchange   = await fetch("exchange")
    by_instrument = await fetch("instrument")
    return {
        "period":        period,
        "by_exchange":   _rows(by_exchange),
        "by_instrument": _rows(by_instrument),
    }


@router.post("/refresh")
async def trigger_stock_refresh(background_tasks: BackgroundTasks):
    """Kick off an equity-perp volume ETL pass in the background."""
    from app.stocks.etl import run_stock_etl_safe
    background_tasks.add_task(run_stock_etl_safe)
    return {"status": "stock etl started"}


@router.post("/hourly-refresh")
async def trigger_stock_hourly_refresh(background_tasks: BackgroundTasks):
    """Kick off an hourly equity-perp volume ETL pass in the background."""
    from app.stocks.hourly_etl import run_stock_hourly_etl_safe
    background_tasks.add_task(run_stock_hourly_etl_safe)
    return {"status": "stock hourly etl started"}
