"""
REST API for historical daily OHLCV data and derived metrics.

Endpoints
---------
GET  /api/history/ohlcv             ?symbol=BTC/USDT[&exchange=binance][&limit=365]
GET  /api/history/metrics                      → ADTV YTD / week / WoW per symbol
GET  /api/history/metrics/exchanges            → same, broken down per exchange
GET  /api/history/weekly-adtv                  → weekly ADTV (RUB) per symbol×exchange×ISO week
GET  /api/history/daily-volume                 → daily volume (RUB), last 30 days
POST /api/history/refresh                      → trigger an incremental OHLCV backfill
POST /api/history/moex-refresh                 → trigger a MOEX FORTS ETL pass
"""

import asyncio
import logging

from fastapi import APIRouter, Query, BackgroundTasks

from app.db.timescale import (
    fetch_ohlcv_daily,
    fetch_history_metrics,
    fetch_history_metrics_by_exchange,
    fetch_weekly_adtv_rub,
    fetch_daily_volume_rub,
    fetch_tradfi_daily_volume,
    fetch_weekly_volume_rub,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/ohlcv")
async def get_daily_ohlcv(
    symbol:   str           = Query(..., description="Canonical symbol, e.g. BTC/USDT"),
    exchange: str | None    = Query(None, description="Exchange ID; omit for aggregated"),
    limit:    int           = Query(365, ge=1, le=1000),
):
    rows = await fetch_ohlcv_daily(symbol, exchange, limit)
    return [dict(r) for r in rows]


@router.get("/metrics")
async def get_metrics():
    """
    Aggregated metrics per symbol (volumes summed across all exchanges per day,
    then averaged).  Includes computed WoW % change.
    """
    rows = await fetch_history_metrics()
    result = []
    for r in rows:
        d = dict(r)
        adtv_week      = float(d["adtv_week"]      or 0)
        adtv_last_week = float(d["adtv_last_week"] or 0)
        if adtv_last_week > 0:
            d["wow_pct"] = round((adtv_week / adtv_last_week - 1) * 100, 2)
        else:
            d["wow_pct"] = None
        # Stringify datetime for JSON serialisation
        if d.get("last_updated"):
            d["last_updated"] = d["last_updated"].isoformat()
        result.append(d)
    return result


@router.get("/metrics/exchanges")
async def get_metrics_by_exchange():
    """Per-exchange ADTV breakdown (used for detail tooltips)."""
    rows = await fetch_history_metrics_by_exchange()
    result = []
    for r in rows:
        d = dict(r)
        adtv_week      = float(d["adtv_week"]      or 0)
        adtv_last_week = float(d["adtv_last_week"] or 0)
        d["wow_pct"] = (
            round((adtv_week / adtv_last_week - 1) * 100, 2)
            if adtv_last_week > 0 else None
        )
        result.append(d)
    return result


@router.get("/weekly-adtv")
async def get_weekly_adtv():
    """
    Weekly ADTV in RUB per symbol × exchange × ISO week.
    Crypto volumes are converted USDT→RUB via daily USDRUBF rate.
    MOEX FORTS appears as exchange='moex' with volumes already in RUB.
    Returns a flat list; the frontend groups and pivots into per-symbol charts.
    """
    rows = await fetch_weekly_adtv_rub()
    return [
        {
            "week_start":   str(r["week_start"]),
            "week_label":   r["week_label"].strip(),
            "symbol":       r["symbol"],
            "exchange":     r["exchange"],
            "days_in_week": r["days_in_week"],
            "adtv":         float(r["adtv"] or 0),
        }
        for r in rows
    ]


@router.get("/daily-volume")
async def get_daily_volume():
    """
    Daily trading volume in RUB per symbol × exchange for the last 30 days.
    Crypto: quote_volume × USDRUBF rate. MOEX: value_rub.
    """
    rows = await fetch_daily_volume_rub()
    return [
        {
            "date":       str(r["date"]),
            "date_label": r["date_label"].strip(),
            "symbol":     r["symbol"],
            "exchange":   r["exchange"],
            "volume_rub": float(r["volume_rub"] or 0),
        }
        for r in rows
    ]


@router.get("/tradfi-volume")
async def get_tradfi_volume():
    """
    Daily trading volume in RUB for tradfi perps only (Commodities, Metals, US Market).
    Per symbol × exchange, last 30 days. Used by TradFi Market Share page.
    """
    rows = await fetch_tradfi_daily_volume()
    return [
        {
            "date":       str(r["date"]),
            "date_label": r["date_label"].strip(),
            "symbol":     r["symbol"],
            "exchange":   r["exchange"],
            "volume_rub": float(r["volume_rub"] or 0),
        }
        for r in rows
    ]


@router.get("/tradfi-weekly-volume")
async def get_tradfi_weekly_volume():
    """
    Weekly SUMMED trading volume in RUB for tradfi perps only (Commodities,
    Metals, US Market). Per symbol × exchange × ISO week, YTD. Weekly view of
    the TradFi Market Share page.
    """
    rows = await fetch_weekly_volume_rub(tradfi_only=True)
    return [
        {
            "date":       str(r["week_start"]),
            "symbol":     r["symbol"],
            "exchange":   r["exchange"],
            "volume_rub": float(r["volume_rub"] or 0),
        }
        for r in rows
    ]


@router.get("/weekly-volume")
async def get_weekly_volume():
    """
    Weekly SUMMED trading volume in RUB per symbol × exchange × ISO week, YTD,
    across all asset classes. Weekly view of the asset-group charts.
    """
    rows = await fetch_weekly_volume_rub(tradfi_only=False)
    return [
        {
            "date":       str(r["week_start"]),
            "symbol":     r["symbol"],
            "exchange":   r["exchange"],
            "volume_rub": float(r["volume_rub"] or 0),
        }
        for r in rows
    ]


@router.post("/refresh")
async def trigger_refresh(background_tasks: BackgroundTasks):
    """Kick off an incremental crypto OHLCV backfill in the background."""
    from app.backfill.ohlcv import run_backfill
    background_tasks.add_task(run_backfill)
    return {"status": "backfill started"}


@router.post("/moex-refresh")
async def trigger_moex_refresh(background_tasks: BackgroundTasks):
    """Kick off a MOEX ETL pass in the background."""
    from app.moex.etl import run_moex_etl
    background_tasks.add_task(run_moex_etl)
    return {"status": "moex etl started"}
