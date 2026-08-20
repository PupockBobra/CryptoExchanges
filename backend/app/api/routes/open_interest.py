"""
Open Interest API

GET /api/open-interest/current  — latest OI per symbol × exchange
GET /api/open-interest/history  — time series (30d default)
GET  /api/open-interest/symbols — list of symbols with stored data
POST /api/open-interest/moex-refresh — trigger a MOEX FORTS OI ETL pass
"""

from fastapi import APIRouter, BackgroundTasks, Query

from app.db.timescale import (
    US_STOCK_CURATED_BASES,
    fetch_oi_latest,
    fetch_oi_history,
    fetch_oi_symbols,
    fetch_oi_daily,
    fetch_oi_equity_bases,
    fetch_top_stock_tickers,
)
from app.stocks.config import (
    EXTRA_DISPLAYED_TICKERS,
    KOREAN_TICKERS,
    TOP_STOCKS_DISPLAYED,
)
from app.api.cache import ttl_cache

router = APIRouter()


async def _dropped_stock_bases() -> set[str]:
    """
    Equity bases to hide on the daily chart: every equity perp the OI table holds
    except the ones on display — the current top N plus the tickers pinned to
    another section.  The top set rotates, so rows written for last month's
    leaders stay in the table and would otherwise pile up as extra US Market
    cards.

    The candidate set comes from `open_interest` itself, not from the tickers the
    stock ETL has written volume rows for: the OI collector sees a new listing on
    its next market scan, while the first daily volume row only lands once the
    venue closes a daily candle.  A volume-based list therefore let every fresh
    equity perp show up as an unranked extra card for a day or more (UNITREE,
    31.07.2026).  Curated US stocks are added explicitly — they live in
    `instruments`, so the equity query does not return them.
    """
    shown = (
        set(await fetch_top_stock_tickers(TOP_STOCKS_DISPLAYED))
        | set(EXTRA_DISPLAYED_TICKERS)
        | set(KOREAN_TICKERS)      # own section — never subject to the US ranking
    )
    seen = set(await fetch_oi_equity_bases()) | set(US_STOCK_CURATED_BASES)
    return seen - shown


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
@ttl_cache()
async def get_daily():
    """Last OI per (day, exchange, symbol) for the last 30 days — for bar charts."""
    rows = await fetch_oi_daily(sorted(await _dropped_stock_bases()))
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


@router.post("/moex-refresh")
async def trigger_moex_oi_refresh(background_tasks: BackgroundTasks):
    """Kick off a MOEX FORTS open-interest ETL pass in the background."""
    from app.moex.oi_etl import run_moex_oi_etl_safe
    background_tasks.add_task(run_moex_oi_etl_safe)
    return {"status": "moex oi etl started"}


@router.get("/symbols")
async def get_symbols():
    symbols = await fetch_oi_symbols()
    return {"symbols": symbols}
