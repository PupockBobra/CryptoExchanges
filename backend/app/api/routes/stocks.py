"""
REST API for equity (stock) perpetual-futures turnover on crypto exchanges.

GET  /api/stocks/volume?period=daily|weekly
        → { "by_exchange": [...], "by_instrument": [...] }
          daily  = last 30 days (bucket = date)
          weekly = ISO weeks since 2026-01-01 (bucket = Monday)
        Each row: { bucket, bucket_label, series, volume_rub }  (RUB).
POST /api/stocks/refresh   → trigger an equity-perp ETL pass in the background.
"""

import logging

from fastapi import APIRouter, Query, BackgroundTasks

from app.db.timescale import fetch_stock_daily_volume, fetch_stock_weekly_volume

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


@router.get("/volume")
async def get_stock_volume(
    period: str = Query("weekly", pattern="^(daily|weekly)$"),
):
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
