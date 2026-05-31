import asyncio
import json
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

                    dead: set[WebSocket] = set()
                    # Snapshot the set to avoid "changed size during iteration"
                    # if a WS connects/disconnects concurrently.
                    for ws in list(clients):
                        try:
                            await ws.send_text(data)
                        except Exception:
                            dead.add(ws)
                    for ws in dead:
                        clients.discard(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error("Broadcast loop inner error: %s", e, exc_info=True)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Broadcast loop disconnected (%s), reconnecting in 2 s…", e)
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
        _ws_clients.get(channel, set()).discard(websocket)
        log.info("WS disconnected: channel=%s", channel)
