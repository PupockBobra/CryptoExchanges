from fastapi import APIRouter, Query
from app.db.timescale import fetch_ohlcv, fetch_latest_ticks, fetch_instruments
from app.config import settings

router = APIRouter()


@router.get("/latest")
async def latest_prices(symbol: str = Query(...)):
    rows = await fetch_latest_ticks(symbol)
    return [dict(r) for r in rows]


@router.get("/ohlcv")
async def ohlcv(
    symbol:   str = Query(..., description="Unified ccxt symbol, e.g. BTC/USDT or XAU/USDT:USDT"),
    exchange: str = Query(...),
    interval: str = Query("1 minute"),
    limit:    int = Query(200, ge=1, le=1000),
):
    rows = await fetch_ohlcv(symbol, exchange, interval, limit)
    return [dict(r) for r in rows]


@router.get("/symbols")
async def list_symbols():
    """Return enabled symbols from DB (falls back to config if table is empty)."""
    rows = await fetch_instruments(enabled_only=True)
    if rows:
        symbols = [r["canonical"] for r in rows]
    else:
        symbols = settings.symbols
    return {"symbols": symbols, "exchanges": settings.exchanges}
