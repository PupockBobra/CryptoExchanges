"""
REST API for historical daily OHLCV data and derived metrics.

Endpoints
---------
GET /api/history/ohlcv    ?symbol=BTC/USDT[&exchange=binance][&limit=365]
GET /api/history/metrics               → ADTV YTD / week / WoW for every symbol
GET /api/history/metrics/exchanges     → same, broken down per exchange
POST /api/history/refresh              → trigger an immediate incremental backfill
"""

import asyncio
import logging

from fastapi import APIRouter, Query, BackgroundTasks

from app.db.timescale import (
    fetch_ohlcv_daily,
    fetch_history_metrics,
    fetch_history_metrics_by_exchange,
    fetch_weekly_adtv,
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
    Weekly ADTV per symbol × exchange × ISO week.
    Returns a flat list; the frontend groups and pivots into per-symbol charts.
    """
    rows = await fetch_weekly_adtv()
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
