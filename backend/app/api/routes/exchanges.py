import json
from fastapi import APIRouter

from app.config import settings
from app.redis_client import get_redis

router = APIRouter()


@router.get("/")
async def list_exchanges():
    """Return latest stats for every configured exchange from Redis hashes."""
    r = await get_redis()
    result = {}
    for ex_id in settings.exchanges:
        raw = await r.hgetall(f"exchange:stats:{ex_id}")
        if raw:
            stats = dict(raw)
            # Coerce numeric fields
            for int_key in ("ticks_total", "ticks_1m", "bytes_in", "reconnects"):
                stats[int_key] = int(stats.get(int_key) or 0)
            # Parse JSON list
            try:
                stats["symbols_active"] = json.loads(stats.get("symbols_active") or "[]")
            except Exception:
                stats["symbols_active"] = []
        else:
            stats = {
                "exchange": ex_id,
                "status": "unknown",
                "ticks_total": 0,
                "ticks_1m": 0,
                "bytes_in": 0,
                "reconnects": 0,
                "symbols_active": [],
                "last_tick_ts": None,
                "started_at": None,
                "updated_at": None,
            }
        result[ex_id] = stats
    return result
