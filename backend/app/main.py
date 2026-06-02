import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.timescale import init_db, seed_instruments_from_config
from app.redis_client import get_redis, close_redis
from app.api.routes import prices, arbitrage, health, instruments, exchanges, history, news, funding, launches
from app.api.routes.launches import launches_refresh_loop
from app.backfill.ohlcv import backfill_loop
from app.backfill.funding import funding_collector_loop
from app.moex.etl import moex_etl_loop

logging.basicConfig(level=settings.log_level)
log = logging.getLogger(__name__)

# Connected WebSocket clients keyed by exact channel name
_ws_clients: dict[str, set[WebSocket]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_instruments_from_config()
    await get_redis()
    asyncio.create_task(_redis_broadcast_loop())
    asyncio.create_task(backfill_loop())
    asyncio.create_task(funding_collector_loop())
    asyncio.create_task(launches_refresh_loop())
    asyncio.create_task(moex_etl_loop())
    log.info("Backend started")
    yield
    await close_redis()
    log.info("Backend stopped")


async def _redis_broadcast_loop():
    """
    Subscribe to all price, stats, and arbitrage channels via pattern subscription
    and fan-out messages to connected WebSocket clients.  Reconnects automatically
    if the Redis connection drops.
    """
    while True:
        pubsub = None
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.psubscribe("prices:*", "stats:*", "arbitrage:*")
            log.info("Redis broadcast loop: psubscribed to prices:*, stats:*, arbitrage:*")

            async for message in pubsub.listen():
                try:
                    if message.get("type") != "pmessage":
                        continue

                    channel: str = message["channel"]
                    data: str    = message["data"]

                    clients = _ws_clients.get(channel)
                    if not clients:
                        continue

                    # Send to all clients concurrently — a single slow client
                    # must not block the rest of the fan-out.
                    snapshot = list(clients)
                    results  = await asyncio.gather(
                        *(ws.send_text(data) for ws in snapshot),
                        return_exceptions=True,
                    )
                    for ws, res in zip(snapshot, results):
                        if isinstance(res, Exception):
                            clients.discard(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error("Broadcast loop inner error: %s", e, exc_info=True)

        except asyncio.CancelledError:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
            raise
        except Exception as e:
            log.warning("Broadcast loop disconnected (%s), reconnecting in 2 s…", e)
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
            await asyncio.sleep(2)


app = FastAPI(title="Crypto Tracker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router,       prefix="/health",           tags=["health"])
app.include_router(prices.router,       prefix="/api/prices",       tags=["prices"])
app.include_router(arbitrage.router,    prefix="/api/arbitrage",    tags=["arbitrage"])
app.include_router(instruments.router,  prefix="/api/instruments",  tags=["instruments"])
app.include_router(exchanges.router,    prefix="/api/exchanges",    tags=["exchanges"])
app.include_router(history.router,      prefix="/api/history",      tags=["history"])
app.include_router(news.router,         prefix="/api/news",         tags=["news"])
app.include_router(funding.router,      prefix="/api/funding",      tags=["funding"])
app.include_router(launches.router,     prefix="/api/launches",     tags=["launches"])


@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str):
    await websocket.accept()
    _ws_clients.setdefault(channel, set()).add(websocket)
    log.info("WS connected: channel=%s total_clients=%d", channel, len(_ws_clients[channel]))
    try:
        while True:
            await websocket.receive_text()   # keep-alive ping/pong
    except WebSocketDisconnect:
        clients = _ws_clients.get(channel)
        if clients is not None:
            clients.discard(websocket)
            if not clients:
                _ws_clients.pop(channel, None)
        log.info("WS disconnected: channel=%s", channel)
