"""
Live order-book feed for SPB perps (Finam TradeAPI).

The book must update continuously, like a trading terminal.  Finam's token
grants brokerage access, so the frontend must never hit Finam directly —
instead this module keeps an in-memory snapshot warm and the API serves that
cache instantly; the frontend polls the cache every second.

Primary feed — **gRPC streaming** (``finam-sdk``): one ``SubscribeOrderBook``
server-stream per ticker, 25 concurrent streams on a single channel.  The
first message is a full snapshot (ACTION_ADD rows), then incremental deltas
(ADD / UPDATE / REMOVE per price level) arrive tick-by-tick.  Verified live:
streaming is NOT subject to the REST 429 rate limits — all 25 subscriptions
run concurrently without throttling.

Fallback feed — the original **REST poller**, used only if the SDK is missing
or the gRPC session fails.  Its rate-limit discipline (learned the hard way —
parallel bursts get 429'd): fetch tickers sequentially with a throttle, one
cycle at a time, ``retries=1``.  A full cycle over 25 tickers is ~15 s.

Both feeds are *lazy*: they only run while the endpoint has been read recently
(someone is on the page), so we don't hold streams / hammer Finam 24/7.  Each
API read renews that active window.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.spb.config import FINAM_MIC, SPB_GROUPS, SPB_NAMES, SPB_TICKERS
from app.spb.fetcher import FinamClient, num_value

try:  # official gRPC SDK; without it the REST fallback below still works
    from finam_trade_api import AsyncFinamClient
    from finam_trade_api.market_data import StreamOrderBook, SubscribeOrderBookRequest
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - dev env without finam-sdk
    _SDK_AVAILABLE = False

log = logging.getLogger(__name__)

_DEPTH = 12                 # levels kept per side
_ACTIVE_WINDOW_SEC = 30.0  # keep the feed running this long after the last read
_IDLE_SLEEP_SEC = 3.0      # nap while nobody is watching
_STREAM_RETRY_SEC = 3.0    # pause before resubscribing a dropped stream

# REST fallback tuning (matches the proven SPB ETL)
_THROTTLE_SEC = 0.5        # pause between individual Finam REST calls
_CYCLE_PAUSE_SEC = 2.0     # pause between full REST refresh cycles

_last_access: float = 0.0
_cycle_lock = asyncio.Lock()


def _placeholder(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "name":   SPB_NAMES.get(ticker, ticker),
        "group":  SPB_GROUPS.get(ticker, "US Market"),
        "bids":   [],
        "asks":   [],
        "error":  None,
        "ts":     None,
    }


# Pre-seed so the API returns all 25 cards immediately (each "loading") instead
# of a blank page while the first snapshot fills them in.
_cache: dict[str, dict] = {t: _placeholder(t) for t in SPB_TICKERS}


def note_access() -> None:
    """Mark the cache as actively read — wakes / sustains the background feed."""
    global _last_access
    _last_access = time.monotonic()


def get_cached_orderbooks() -> list[dict]:
    """Current cached books in display order.  Renews the active window."""
    note_access()
    return [_cache[t] for t in SPB_TICKERS]


def _active() -> bool:
    return time.monotonic() - _last_access <= _ACTIVE_WINDOW_SEC


# --------------------------------------------------------------------------
# Primary feed: gRPC streaming (tick-by-tick deltas)
# --------------------------------------------------------------------------

def _dec(d) -> float:
    """google.type.Decimal → float (its ``value`` field is a string)."""
    try:
        return float(d.value)
    except (TypeError, ValueError):
        return 0.0


def _render(ticker: str, bids: dict[float, float], asks: dict[float, float]) -> dict:
    return {
        "ticker": ticker,
        "name":   SPB_NAMES.get(ticker, ticker),
        "group":  SPB_GROUPS.get(ticker, "US Market"),
        "bids":   [{"price": p, "size": s} for p, s in sorted(bids.items(), reverse=True)[:_DEPTH]],
        "asks":   [{"price": p, "size": s} for p, s in sorted(asks.items())[:_DEPTH]],
        "error":  None,
        "ts":     datetime.now(timezone.utc).isoformat(),
    }


async def _consume_stream(client, ticker: str) -> None:
    """One subscription: apply the snapshot, then deltas, re-rendering the cache
    on every message.  Book state lives here so every (re)subscribe starts
    clean from the server's fresh snapshot."""
    remove = StreamOrderBook.Row.Action.ACTION_REMOVE
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    async for resp in client.market_data.SubscribeOrderBook(
        SubscribeOrderBookRequest(symbol=f"{ticker}@{FINAM_MIC}")
    ):
        for ob in resp.order_book:
            for row in ob.rows:
                is_bid = row.HasField("buy_size")
                side = bids if is_bid else asks
                price = _dec(row.price)
                if row.action == remove:
                    side.pop(price, None)
                else:  # ADD / UPDATE — upsert the price level
                    side[price] = _dec(row.buy_size if is_bid else row.sell_size)
        _cache[ticker] = _render(ticker, bids, asks)


async def _stream_ticker(client, ticker: str) -> None:
    """Keep one ticker subscribed for the whole session; if the server closes
    the stream (or it errors), resubscribe after a short pause.  Previous
    levels stay in the cache so the card never blanks."""
    while True:
        try:
            await _consume_stream(client, ticker)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep last book, resubscribe
            prev = _cache.get(ticker) or _placeholder(ticker)
            _cache[ticker] = {**prev, "error": str(exc)}
            log.warning("SPB orderbook stream %s dropped: %s", ticker, exc)
        await asyncio.sleep(_STREAM_RETRY_SEC)


async def _stream_session() -> None:
    """One gRPC session per active window: 25 concurrent server-streams on a
    single channel.  The SDK refreshes the JWT in the background; the channel
    and all subscriptions are torn down when the viewer leaves."""
    async with AsyncFinamClient(secret=settings.finam_api_token) as client:
        tasks = [asyncio.create_task(_stream_ticker(client, t)) for t in SPB_TICKERS]
        try:
            while _active():
                await asyncio.sleep(1.0)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------
# Fallback feed: sequential REST polling (~15 s per full cycle)
# --------------------------------------------------------------------------

def _parse(ticker: str, ob: dict) -> dict:
    bids, asks = [], []
    for r in ob.get("rows", []):
        price = num_value(r, "price")
        if r.get("buy_size") is not None:
            bids.append({"price": price, "size": num_value(r, "buy_size")})
        elif r.get("sell_size") is not None:
            asks.append({"price": price, "size": num_value(r, "sell_size")})
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])
    return {
        "ticker": ticker,
        "name":   SPB_NAMES.get(ticker, ticker),
        "group":  SPB_GROUPS.get(ticker, "US Market"),
        "bids":   bids[:_DEPTH],
        "asks":   asks[:_DEPTH],
        "error":  None,
        "ts":     datetime.now(timezone.utc).isoformat(),
    }


async def _refresh_cycle(client: FinamClient) -> None:
    """Refresh every ticker once, sequentially with a throttle.  A per-ticker
    failure keeps the previous levels (so the UI never blanks) and records the
    error.  Stops early if the viewer leaves mid-cycle."""
    async with _cycle_lock:
        for ticker in SPB_TICKERS:
            if not _active():
                return
            try:
                ob = await client.fetch_orderbook(ticker, retries=1)
                _cache[ticker] = _parse(ticker, ob)
            except Exception as exc:  # noqa: BLE001 — keep last book, record error
                prev = _cache.get(ticker) or _placeholder(ticker)
                _cache[ticker] = {**prev, "error": str(exc)}
            await asyncio.sleep(_THROTTLE_SEC)


async def _rest_poll_window() -> None:
    """REST-poll the books for the rest of the current active window.  One
    client for the whole window: caches the JWT and reuses the connection
    pool across cycles."""
    async with FinamClient(settings.finam_api_token) as client:
        while _active():
            await _refresh_cycle(client)
            await asyncio.sleep(_CYCLE_PAUSE_SEC)


# --------------------------------------------------------------------------
# Entry point (spawned from main.py lifespan)
# --------------------------------------------------------------------------

async def spb_orderbook_poll_loop() -> None:
    """Background task: while the page is being viewed, keep the cache live —
    gRPC streaming first, REST polling as fallback."""
    if not settings.finam_api_token:
        log.info("SPB orderbook feed disabled (no Finam token)")
        return
    log.info("SPB orderbook feed started (grpc streaming available: %s)", _SDK_AVAILABLE)
    while True:
        if not _active():
            await asyncio.sleep(_IDLE_SLEEP_SEC)
            continue
        if _SDK_AVAILABLE:
            try:
                await _stream_session()
                continue  # window went idle — back to napping
            except Exception as exc:  # noqa: BLE001 — degrade to REST for this window
                log.warning("SPB orderbook gRPC session failed (%s) — falling back to REST", exc)
        try:
            await _rest_poll_window()
        except Exception as exc:  # noqa: BLE001 — self-heal on dropped pool / stale JWT
            log.warning("SPB orderbook poll cycle failed: %s", exc)
            await asyncio.sleep(5.0)
