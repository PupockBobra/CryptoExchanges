from fastapi import APIRouter, Query
from app.db.timescale import fetch_recent_alerts

router = APIRouter()


@router.get("/alerts")
async def recent_alerts(
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    rows = await fetch_recent_alerts(symbol, limit)
    return [dict(r) for r in rows]
