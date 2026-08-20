"""
REST API for SPB Exchange perpetual-futures turnover (sourced from Finam TradeAPI).

Endpoints
---------
GET  /api/spb/daily-volume   → daily turnover (RUB) per ticker, last 30 days
GET  /api/spb/open-interest  → daily open interest per ticker, last 30 days
POST /api/spb/refresh        → trigger a Finam turnover ETL pass in the background
POST /api/spb/oi-refresh     → trigger an SPB-API open-interest ETL pass
"""

import logging

from fastapi import APIRouter, BackgroundTasks

from app.config import settings
from app.db.timescale import (
    fetch_spb_daily_volume,
    fetch_spb_oi_daily,
    fetch_spb_weekly_adtv,
)
from app.spb.config import SPB_GROUPS, SPB_NAMES

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/daily-volume")
async def get_spb_daily_volume():
    """
    Daily SPB perp turnover in RUB per ticker for the last 30 days.
    Turnover is USD×USDRUBF; the current day is exact, history is approximated.
    """
    rows = await fetch_spb_daily_volume()
    return [
        {
            "date":         str(r["date"]),
            "date_label":   r["date_label"].strip(),
            "ticker":       r["ticker"],
            "name":         SPB_NAMES.get(r["ticker"], r["ticker"]),
            "group":        SPB_GROUPS.get(r["ticker"], "US Market"),
            "turnover_rub": float(r["turnover_rub"] or 0),
        }
        for r in rows
    ]


@router.get("/weekly-adtv")
async def get_spb_weekly_adtv():
    """
    Weekly ADTV in RUB per SPB ticker × ISO week.
    Same RUB conversion as /daily-volume; the frontend pivots into per-ticker charts.
    """
    rows = await fetch_spb_weekly_adtv()
    return [
        {
            "week_start":   str(r["week_start"]),
            "week_label":   r["week_label"].strip(),
            "ticker":       r["ticker"],
            "name":         SPB_NAMES.get(r["ticker"], r["ticker"]),
            "group":        SPB_GROUPS.get(r["ticker"], "US Market"),
            "days_in_week": r["days_in_week"],
            "adtv":         float(r["adtv"] or 0),
        }
        for r in rows
    ]


@router.get("/open-interest")
async def get_spb_open_interest():
    """
    Daily SPB perp open interest per ticker for the last 30 days.
    oi_rub is the open-position notional (USD×USDRUBF); oi_contracts is raw contracts.
    """
    rows = await fetch_spb_oi_daily()
    return [
        {
            "date":         str(r["date"]),
            "date_label":   r["date_label"].strip(),
            "ticker":       r["ticker"],
            "name":         SPB_NAMES.get(r["ticker"], r["ticker"]),
            "group":        SPB_GROUPS.get(r["ticker"], "US Market"),
            "oi_contracts": float(r["oi_contracts"] or 0),
            "oi_rub":       float(r["oi_rub"] or 0),
        }
        for r in rows
    ]


@router.get("/orderbook")
async def get_spb_orderbook():
    """
    Live order book for every SPB perp, served from the in-memory cache kept
    warm by the background poller (``app.spb.orderbook``) — not a per-request
    Finam call.  Reading renews the poller's active window, so it keeps
    refreshing while the page is open and idles otherwise.

    Prices are in USD (the instrument's quote currency), sizes in contracts.
    The cache is pre-seeded with placeholder cards, so the first load renders all
    instruments immediately and the poller fills their levels progressively.
    """
    from app.spb.orderbook import get_cached_orderbooks

    if not settings.finam_api_token:
        return []

    return get_cached_orderbooks()


@router.post("/refresh")
async def trigger_spb_refresh(background_tasks: BackgroundTasks):
    """Kick off a Finam SPB turnover ETL pass in the background."""
    from app.spb.etl import run_spb_etl
    background_tasks.add_task(run_spb_etl)
    return {"status": "spb etl started"}


@router.post("/oi-refresh")
async def trigger_spb_oi_refresh(background_tasks: BackgroundTasks):
    """Kick off an SPB-API open-interest ETL pass in the background."""
    from app.spb.oi_etl import run_spb_oi_etl
    background_tasks.add_task(run_spb_oi_etl)
    return {"status": "spb oi etl started"}
