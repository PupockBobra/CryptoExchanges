import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.timescale import init_db, seed_instruments_from_config
from app.redis_client import get_redis, close_redis
from app.api.routes import prices, arbitrage, health, instruments, exchanges, history, news, funding, launches
from app.api.routes import open_interest, spb, stocks, reports, mm, crypto_top, mmdetect, okr
from app.api.routes.launches import launches_refresh_loop
from app.backfill.ohlcv import backfill_loop
from app.backfill.hourly import hourly_backfill_loop
from app.backfill.funding import funding_collector_loop
from app.moex.etl import moex_etl_loop
from app.moex.oi_etl import moex_oi_etl_loop
from app.spb.etl import spb_etl_loop
from app.spb.funding_exchange import spb_funding_exchange_loop
from app.spb.funding_tg import spb_funding_tg_loop
from app.spb.oi_etl import spb_oi_etl_loop
from app.spb.orderbook import spb_orderbook_poll_loop
from app.spb.spread_etl import spb_spread_collector_loop
from app.stocks.etl import stock_etl_loop
from app.stocks.hourly_etl import stock_hourly_etl_loop
from app.crypto.etl import crypto_daily_etl_loop, crypto_hourly_etl_loop
from app.oi.etl import oi_collector_loop
from app.mm.orderbook import mm_orderbook_poll_loop
from app.mm.spread_etl import mm_spread_collector_loop
from app.mmdetect.collector import mmdetect_collector_loop
from app.okr.etl import okr_etl_loop

logging.basicConfig(level=settings.log_level)
log = logging.getLogger(__name__)


class _CcxtNoiseFilter(logging.Filter):
    """Drop cosmetic ccxt/aiohttp warnings about lazily-created HTTP sessions.

    Why: ccxt hyperliquid creates internal aiohttp sessions on-demand that
    aren't tracked by the main exchange.close() call. Python GC reclaims the
    sockets correctly (verified: backend FD count stays at ~13), but the
    __del__ method spams 'Unclosed client session' / 'Unclosed connector'
    warnings on every backfill pass, drowning useful logs.
    """
    _NOISE = (
        "requires to release all resources",
        "Unclosed client session",
        "Unclosed connector",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(needle in msg for needle in self._NOISE)


for _name in ("ccxt.base.exchange", "asyncio"):
    logging.getLogger(_name).addFilter(_CcxtNoiseFilter())

# Connected WebSocket clients keyed by exact channel name
_ws_clients: dict[str, set[WebSocket]] = {}

# Strong references to the long-lived background tasks.  asyncio keeps only weak
# references to tasks created via create_task(), so a fire-and-forget task can be
# garbage-collected mid-flight — which silently killed the SPB ETL loop, leaving
# its data to go stale.  Holding the references here prevents that.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """Create a background task and keep a strong reference to it."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_instruments_from_config()
    await get_redis()
    _spawn(_redis_broadcast_loop())
    _spawn(backfill_loop())
    _spawn(hourly_backfill_loop())
    _spawn(funding_collector_loop())
    _spawn(launches_refresh_loop())
    _spawn(moex_etl_loop())
    _spawn(moex_oi_etl_loop())
    _spawn(spb_etl_loop())
    _spawn(spb_funding_exchange_loop())
    _spawn(spb_funding_tg_loop())
    _spawn(spb_oi_etl_loop())
    _spawn(spb_orderbook_poll_loop())
    _spawn(spb_spread_collector_loop())
    _spawn(stock_etl_loop())
    _spawn(stock_hourly_etl_loop())
    _spawn(crypto_daily_etl_loop())
    _spawn(crypto_hourly_etl_loop())
    _spawn(oi_collector_loop())
    _spawn(mm_orderbook_poll_loop())
    _spawn(mm_spread_collector_loop())
    _spawn(mmdetect_collector_loop())
    _spawn(okr_etl_loop())
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
app.include_router(launches.router,         prefix="/api/launches",         tags=["launches"])
app.include_router(open_interest.router,    prefix="/api/open-interest",    tags=["open-interest"])
app.include_router(spb.router,              prefix="/api/spb",              tags=["spb"])
app.include_router(stocks.router,           prefix="/api/stocks",           tags=["stocks"])
app.include_router(crypto_top.router,       prefix="/api/crypto-top",       tags=["crypto-top"])
app.include_router(okr.router,               prefix="/api/okr",              tags=["okr"])
app.include_router(reports.router,          prefix="/api/reports",          tags=["reports"])
app.include_router(mm.router,               prefix="/api/mm",               tags=["mm"])
app.include_router(mmdetect.router,         prefix="/api/mmdetect",         tags=["mmdetect"])


@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str):
    await websocket.accept()
    _ws_clients.setdefault(channel, set()).add(websocket)
    log.info("WS connected: channel=%s total_clients=%d", channel, len(_ws_clients[channel]))
    try:
        while True:
            await websocket.receive_text()   # keep-alive ping/pong
    except WebSocketDisconnect:
        log.info("WS disconnected: channel=%s", channel)
    finally:
        # Deregister on ANY exit path — a non-disconnect exception must not
        # leave a dead socket in the broadcast fan-out set.
        clients = _ws_clients.get(channel)
        if clients is not None:
            clients.discard(websocket)
            if not clients:
                _ws_clients.pop(channel, None)
