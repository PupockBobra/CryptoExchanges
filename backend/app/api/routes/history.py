"""
REST API for historical daily OHLCV data and derived metrics.

Endpoints
---------
GET  /api/history/ohlcv             ?symbol=BTC/USDT[&exchange=binance][&limit=365]
GET  /api/history/metrics                      → ADTV YTD / week / WoW per symbol
GET  /api/history/metrics/exchanges            → same, broken down per exchange
GET  /api/history/weekly-adtv                  → weekly ADTV (RUB) per symbol×exchange×ISO week
GET  /api/history/daily-volume                 → daily volume (RUB), last 30 days
GET  /api/history/hourly-volume                → hourly volume (RUB) time series
GET  /api/history/hourly-profile               → mean volume (RUB) per MSK hour-of-day
POST /api/history/refresh                      → trigger an incremental OHLCV backfill
POST /api/history/moex-refresh                 → trigger a MOEX FORTS ETL pass
"""

import asyncio
import logging

from fastapi import APIRouter, Query, BackgroundTasks

from app.db.timescale import (
    US_STOCK_CURATED_BASES,
    fetch_ohlcv_daily,
    fetch_history_metrics,
    fetch_history_metrics_by_exchange,
    fetch_weekly_adtv_rub,
    fetch_daily_volume_rub,
    fetch_hourly_volume_rub,
    fetch_hourly_profile_rub,
    fetch_stock_daily_volume_rub,
    fetch_stock_weekly_adtv_rub,
    fetch_top_stock_tickers,
    fetch_tradfi_daily_volume,
    fetch_weekly_volume_rub,
)
from app.stocks.config import EXTRA_DISPLAYED_TICKERS, TOP_STOCKS_DISPLAYED
from app.api.cache import ttl_cache

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
@ttl_cache()
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
@ttl_cache()
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


async def _displayed_stock_tickers() -> list[str]:
    """Equity perps to serve: the US Market top N plus the tickers pinned to
    another section (SK Hynix ADR renders with the Korean cards)."""
    top = await fetch_top_stock_tickers(TOP_STOCKS_DISPLAYED)
    return top + [t for t in EXTRA_DISPLAYED_TICKERS if t not in top]


@router.get("/us-stock-tickers")
@ttl_cache()
async def get_us_stock_tickers():
    """
    The equity-perp tickers currently shown in the US Market section — top N by
    turnover over the last complete ISO week.  The set rotates with the ranking,
    so the frontend reads it from here instead of a hard-coded list.
    """
    return {"tickers": await fetch_top_stock_tickers(TOP_STOCKS_DISPLAYED)}


@router.get("/weekly-adtv")
@ttl_cache()
async def get_weekly_adtv():
    """
    Weekly ADTV in RUB per symbol × exchange × ISO week.
    Crypto volumes are converted USDT→RUB via daily USDRUBF rate.
    MOEX FORTS appears as exchange='moex' with volumes already in RUB.
    US stocks come from the stock ETL (top N by weekly turnover) instead of the
    curated instruments, which are excluded so they aren't counted twice.
    Returns a flat list; the frontend groups and pivots into per-symbol charts.
    """
    tickers = await _displayed_stock_tickers()
    rows = await fetch_weekly_adtv_rub(exclude_bases=US_STOCK_CURATED_BASES)
    rows = list(rows) + list(await fetch_stock_weekly_adtv_rub(tickers))
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
@ttl_cache()
async def get_daily_volume():
    """
    Daily trading volume in RUB per symbol × exchange for the last 30 days.
    Crypto: quote_volume × USDRUBF rate. MOEX: value_rub.
    US stocks: top N equity perps from the stock ETL (see /weekly-adtv).
    """
    tickers = await _displayed_stock_tickers()
    rows = await fetch_daily_volume_rub(exclude_bases=US_STOCK_CURATED_BASES)
    rows = list(rows) + list(await fetch_stock_daily_volume_rub(tickers))
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


def pivot_to_series(
    rows,
    axis_of,
    value_of,
    axis: list | None = None,
) -> tuple[list, list[dict]]:
    """
    Reshape flat (axis, symbol, exchange, value) rows into a shared axis plus one
    array of values per symbol × exchange.

    A week of hourly bars is ~29 000 rows, and as objects the repeated field
    names and symbol strings dominate the payload (1.9 MB for half a window).
    Parallel arrays cut that ~10x — the same trick the spread-history endpoints
    use, and Plotly consumes arrays directly either way.

    `axis` pins the categories (the profile always wants all 24 hours); when
    omitted it is taken from the data in first-seen order, so callers must feed
    rows already sorted along the axis.

    Missing points become `None` rather than 0 — a pair listed midway through the
    window has no volume there, which is not the same as zero volume.
    """
    if axis is None:
        axis = list(dict.fromkeys(axis_of(r) for r in rows))
    index = {a: i for i, a in enumerate(axis)}

    series: dict[tuple[str, str], list] = {}
    for r in rows:
        pos = index.get(axis_of(r))
        if pos is None:
            continue
        key = (r["symbol"], r["exchange"])
        values = series.get(key)
        if values is None:
            values = series[key] = [None] * len(axis)
        values[pos] = value_of(r)

    return axis, [
        {"symbol": sym, "exchange": ex, "values": values}
        for (sym, ex), values in series.items()
    ]


@router.get("/hourly-volume")
@ttl_cache()
async def get_hourly_volume(days: int = Query(7, ge=1, le=30)):
    """
    Hourly trading volume in RUB per symbol × exchange, crypto exchanges only.
    The axis is UTC — the frontend shifts it to MSK like the spread charts do.
    """
    rows = await fetch_hourly_volume_rub(days)
    axis, series = pivot_to_series(
        rows,
        axis_of=lambda r: r["ts"].isoformat(),
        value_of=lambda r: float(r["volume_rub"] or 0),
    )
    return {"days": days, "axis": axis, "series": series}


@router.get("/hourly-profile")
@ttl_cache()
async def get_hourly_profile(days: int = Query(30, ge=1, le=90)):
    """
    Intraday profile: mean volume in RUB per MSK hour-of-day × symbol × exchange.
    Answers "which hours carry the liquidity", with day-to-day noise averaged out.
    """
    rows = await fetch_hourly_profile_rub(days)
    axis, series = pivot_to_series(
        rows,
        axis_of=lambda r: r["hour_msk"],
        value_of=lambda r: float(r["avg_volume_rub"] or 0),
        axis=list(range(24)),
    )
    return {"days": days, "axis": axis, "series": series}


@router.get("/tradfi-volume")
@ttl_cache()
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
@ttl_cache()
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
@ttl_cache()
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
