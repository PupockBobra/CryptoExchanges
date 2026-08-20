"""
Open Interest API

GET /api/open-interest/current  — latest OI per symbol × exchange
GET /api/open-interest/history  — time series (30d default)
GET /api/open-interest/symbols  — list of symbols with stored data
"""

from fastapi import APIRouter, Query

from app.db.timescale import fetch_oi_latest, fetch_oi_history, fetch_oi_symbols, fetch_oi_daily

router = APIRouter()


@router.get("/current")
async def get_current():
    rows = await fetch_oi_latest()
    return {
        "entries": [
            {
                "ts":           row["ts"].isoformat(),
                "exchange":     row["exchange"],
                "symbol":       row["symbol"],
                "oi_contracts": row["oi_contracts"],
                "oi_usdt":      row["oi_usdt"],
            }
            for row in rows
        ]
    }


@router.get("/history")
async def get_history(
    symbol:   str        = Query(..., description="Canonical symbol, e.g. BTC/USDT:USDT"),
    exchange: str | None = Query(None, description="Filter by exchange"),
    days:     int        = Query(30, ge=1, le=90, description="Look-back window in days"),
):
    rows = await fetch_oi_history(symbol, exchange=exchange, days=days)
    return {
        "symbol":   symbol,
        "exchange": exchange,
        "days":     days,
        "data": [
            {
                "ts":           row["ts"].isoformat(),
                "exchange":     row["exchange"],
                "oi_contracts": row["oi_contracts"],
                "oi_usdt":      row["oi_usdt"],
            }
            for row in rows
        ],
    }


@router.get("/daily")
async def get_daily():
    """Last OI per (day, exchange, symbol) for the last 30 days — for bar charts."""
    rows = await fetch_oi_daily()
    return [
        {
            "date":         row["date"].isoformat(),
            "date_label":   row["date_label"],
            "exchange":     row["exchange"],
            "symbol":       row["symbol"],
            "oi_contracts": row["oi_contracts"],
            "oi_usdt":      row["oi_usdt"],
            "oi_rub":       float(row["oi_rub"]) if row["oi_rub"] is not None else None,
        }
        for row in rows
    ]


@router.get("/symbols")
async def get_symbols():
    symbols = await fetch_oi_symbols()
    return {"symbols": symbols}
