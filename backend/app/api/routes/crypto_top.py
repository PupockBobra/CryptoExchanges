"""
REST API for top-N crypto-perp turnover per exchange.

GET  /api/crypto-top/volume?period=daily|weekly|hourly
        → { "period": …, "by_exchange": [...] }
          daily  = last 30 days (bucket = date)
          weekly = ISO weeks since 2026-01-01 (bucket = Monday)
          hourly = mean turnover per MSK hour-of-day over the last 30 days
                   (bucket = '00'…'23')
        Each row: { bucket, bucket_label, series, volume_rub }  (RUB).

        Only the by-exchange aggregate is served: this feeds the Cryptocurrencies
        slice of the asset-group charts, which sums it anyway — a per-coin
        breakdown of ~600 series would be payload nobody renders.
POST /api/crypto-top/refresh  → run both ETL passes in the background.
"""

import logging

from fastapi import APIRouter, Query, BackgroundTasks

from app.db.timescale import (
    fetch_crypto_top_daily,
    fetch_crypto_top_weekly,
    fetch_crypto_top_hourly_profile,
)
from app.api.cache import ttl_cache

log = logging.getLogger(__name__)
router = APIRouter()

HOURLY_PROFILE_DAYS = 30


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


def _hour_rows(records) -> list[dict]:
    """Hour buckets are zero-padded so they sort as plain strings, like dates."""
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
async def get_crypto_top_volume(
    period: str = Query("daily", pattern="^(daily|weekly|hourly)$"),
):
    if period == "hourly":
        return {
            "period": period,
            "by_exchange": _hour_rows(
                await fetch_crypto_top_hourly_profile("exchange", HOURLY_PROFILE_DAYS)
            ),
        }
    fetch = fetch_crypto_top_weekly if period == "weekly" else fetch_crypto_top_daily
    return {"period": period, "by_exchange": _rows(await fetch("exchange"))}


@router.post("/refresh")
async def trigger_crypto_top_refresh(background_tasks: BackgroundTasks):
    """Kick off both top-N crypto ETL passes in the background."""
    from app.crypto.etl import run_crypto_etl_safe
    background_tasks.add_task(run_crypto_etl_safe)
    return {"status": "crypto top etl started"}
