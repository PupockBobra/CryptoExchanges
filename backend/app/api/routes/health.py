from fastapi import APIRouter
from app.redis_client import get_redis
from app.db.timescale import get_pool

router = APIRouter()


@router.get("")
async def health():
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    try:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    status = "ok" if (redis_ok and db_ok) else "degraded"
    return {"status": status, "redis": redis_ok, "db": db_ok}
