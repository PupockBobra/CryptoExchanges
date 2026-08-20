"""
Live order-book feed for MM FORTS futures (Finam TradeAPI, MIC ``RTSX``).

Same design as the SPB Order Book feed (``app.spb.orderbook``): a warm in-memory
cache is kept up to date by a gRPC stream (tick-by-tick deltas) with a REST
poller as fallback, and the API serves the cache instantly.  Two differences:

  * the instrument set is the ISS-resolved MM universe (front-month per
    underlying), not a hard-coded list;
  * it is **lazy per group** — only the FORTS collection whose tab is currently
    open is streamed, so opening "Валюты" doesn't also stream 87 share books.

The spread math is reused unchanged from the SPB feed: passing ``lot=step_ratio``
and ``usdrub=1.0`` makes ``avg_spread_on_volume`` measure the 1 000 000 ₽ depth
via STEPPRICE/MINSTEP and return the absolute spread in the instrument's own
quote unit (₽ / $ / points); the percentage metric is unit-free.
"""

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.mm.config import MM_MIC, MM_TARGET_RUB
from app.mm.universe import ensure_universe, get_universe
from app.spb.fetcher import FinamClient
from app.spb.orderbook import (
    _dec,
    avg_spread_on_volume,
    parse_levels,
    spread_pct_on_volume,
)

try:  # official gRPC SDK; without it the REST fallback still works
    from finam_trade_api import AsyncFinamClient
    from finam_trade_api.market_data import StreamOrderBook, SubscribeOrderBookRequest
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — dev env without finam-sdk
    _SDK_AVAILABLE = False

log = logging.getLogger(__name__)

_DEPTH = 12                 # levels served to the frontend per side (the cache
                            # keeps FULL depth — the spread math needs it)
_ACTIVE_WINDOW_SEC = 30.0
_IDLE_SLEEP_SEC = 3.0
_STREAM_RETRY_SEC = 3.0
_STREAM_SILENCE_SEC = 30.0
_STREAM_CONNECT_TIMEOUT_SEC = 15.0
_STREAM_CLOSE_TIMEOUT_SEC = 10.0
_THROTTLE_SEC = 0.5
_CYCLE_PAUSE_SEC = 2.0

# ticker → book dict; ticker (ASSETCODE) is unique across all FORTS groups.
_cache: dict[str, dict] = {}
# group_id → monotonic time of the last API read (per-group lazy activation).
_last_access: dict[str, float] = {}
_last_stream_msg: float = 0.0
_cycle_lock = asyncio.Lock()


def note_access(group_id: str) -> None:
    """Mark a group's tab as actively read — wakes / sustains its feed."""
    _last_access[group_id] = time.monotonic()


def _active_groups() -> set[str]:
    now = time.monotonic()
    return {g for g, t in _last_access.items() if now - t <= _ACTIVE_WINDOW_SEC}


def _meta(inst: dict) -> dict:
    return {"ticker": inst["ticker"], "name": inst["name"], "group": inst["group"],
            "currency": inst["currency"]}


def _placeholder(inst: dict) -> dict:
    return {**_meta(inst), "bids": [], "asks": [], "error": None, "ts": None}


def _render(inst: dict, bids: dict[float, float], asks: dict[float, float]) -> dict:
    return {
        **_meta(inst),
        "bids": [{"price": p, "size": s} for p, s in sorted(bids.items(), reverse=True)],
        "asks": [{"price": p, "size": s} for p, s in sorted(asks.items())],
        "error": None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _parse(inst: dict, ob: dict) -> dict:
    bids, asks = parse_levels(ob)
    return {**_meta(inst), "bids": bids, "asks": asks, "error": None,
            "ts": datetime.now(timezone.utc).isoformat()}


def get_cached_orderbooks(group_id: str) -> list[dict]:
    """Cached books for one group, renewing its active window, sides cut to
    display depth.  Books not yet filled are returned as placeholders so the UI
    shows all cards immediately.

    The cut happens **here and only here**: the streams keep the full book in
    ``_cache`` because ``compute_live_spreads`` has to walk it to reach the
    1 млн ₽ depth target.  Serving the full book instead cost ~135 KB per poll on
    the 65-instrument shares tab (polled once a second) to render 8 levels."""
    note_access(group_id)
    out = []
    for inst in get_universe():
        if inst["group"] != group_id:
            continue
        bk = _cache.get(inst["ticker"]) or _placeholder(inst)
        out.append({**bk, "bids": bk["bids"][:_DEPTH], "asks": bk["asks"][:_DEPTH]})
    return out


def compute_live_spreads(group_id: str) -> list[dict]:
    """Instantaneous absolute + % spread per ticker in a group, from the current
    cached books (tick-by-tick fresh while streaming).  Absolute is in the
    instrument's quote unit; tickers with an empty book are omitted."""
    out = []
    for inst in get_universe():
        if inst["group"] != group_id:
            continue
        book = _cache.get(inst["ticker"])
        if not book or (not book["bids"] and not book["asks"]):
            continue
        r = inst["step_ratio"]
        out.append({
            "ticker":     inst["ticker"],
            "spread_abs": avg_spread_on_volume(book["bids"], book["asks"], r, 1.0, MM_TARGET_RUB),
            "spread_pct": spread_pct_on_volume(book["bids"], book["asks"], r, 1.0, MM_TARGET_RUB),
        })
    return out


# ── gRPC streaming (primary) ──────────────────────────────────────────────────

async def _consume_stream(client, inst: dict) -> None:
    global _last_stream_msg
    remove = StreamOrderBook.Row.Action.ACTION_REMOVE
    symbol = f"{inst['secid']}@{MM_MIC}"
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    async for resp in client.market_data.SubscribeOrderBook(
        SubscribeOrderBookRequest(symbol=symbol)
    ):
        _last_stream_msg = time.monotonic()
        for ob in resp.order_book:
            for row in ob.rows:
                is_bid = row.HasField("buy_size")
                side = bids if is_bid else asks
                price = _dec(row.price)
                if row.action == remove:
                    side.pop(price, None)
                else:
                    side[price] = _dec(row.buy_size if is_bid else row.sell_size)
        _cache[inst["ticker"]] = _render(inst, bids, asks)


async def _stream_ticker(client, inst: dict) -> None:
    while True:
        try:
            await _consume_stream(client, inst)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep last book, resubscribe
            prev = _cache.get(inst["ticker"]) or _placeholder(inst)
            _cache[inst["ticker"]] = {**prev, "error": str(exc)}
            log.warning("MM orderbook stream %s dropped: %s", inst["secid"], exc)
        await asyncio.sleep(_STREAM_RETRY_SEC)


async def _stream_session(insts: list[dict], active_key: frozenset) -> None:
    """One gRPC session streaming exactly ``insts``.  Returns when the active
    group set changes (so the caller rebuilds), the window goes idle, or the
    silence watchdog trips.  Every phase is time-bounded."""
    global _last_stream_msg
    client = AsyncFinamClient(secret=settings.finam_api_token)
    try:
        await asyncio.wait_for(client.__aenter__(), _STREAM_CONNECT_TIMEOUT_SEC)
    except BaseException:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.__aexit__(None, None, None), _STREAM_CLOSE_TIMEOUT_SEC)
        raise
    tasks: list[asyncio.Task] = []
    try:
        _last_stream_msg = time.monotonic()
        tasks = [asyncio.create_task(_stream_ticker(client, i)) for i in insts]
        while _active_groups() and _active_key() == active_key:
            await asyncio.sleep(1.0)
            if time.monotonic() - _last_stream_msg > _STREAM_SILENCE_SEC:
                raise RuntimeError(f"no stream messages for {_STREAM_SILENCE_SEC:.0f}s")
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                   _STREAM_CLOSE_TIMEOUT_SEC)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.__aexit__(None, None, None), _STREAM_CLOSE_TIMEOUT_SEC)


# ── REST polling (fallback) ───────────────────────────────────────────────────

async def _refresh_cycle(client: FinamClient, insts: list[dict], active_key: frozenset) -> None:
    async with _cycle_lock:
        for inst in insts:
            if not _active_groups() or _active_key() != active_key:
                return
            try:
                ob = await client.fetch_orderbook(inst["secid"], mic=MM_MIC, retries=1)
                _cache[inst["ticker"]] = _parse(inst, ob)
            except Exception as exc:  # noqa: BLE001 — keep last book, record error
                prev = _cache.get(inst["ticker"]) or _placeholder(inst)
                _cache[inst["ticker"]] = {**prev, "error": str(exc)}
            await asyncio.sleep(_THROTTLE_SEC)


async def _rest_window(insts: list[dict], active_key: frozenset) -> None:
    async with FinamClient(settings.finam_api_token) as client:
        while _active_groups() and _active_key() == active_key:
            await _refresh_cycle(client, insts, active_key)
            await asyncio.sleep(_CYCLE_PAUSE_SEC)


# ── active-set helpers + entry point ──────────────────────────────────────────

def _active_insts() -> list[dict]:
    groups = _active_groups()
    return [i for i in get_universe() if i["group"] in groups]


def _active_key() -> frozenset:
    """Identity of the currently-streamed set — the secids of all active groups'
    instruments.  Changing tab (different group) changes this, triggering a
    session rebuild."""
    return frozenset(i["secid"] for i in _active_insts())


async def mm_orderbook_poll_loop() -> None:
    """Background task: keep the active group's books live — gRPC first, REST
    fallback.  Naps while no MM tab is open."""
    if not settings.finam_api_token:
        log.info("MM orderbook feed disabled (no Finam token)")
        return
    log.info("MM orderbook feed started (grpc streaming available: %s)", _SDK_AVAILABLE)
    while True:
        if not _active_groups():
            await asyncio.sleep(_IDLE_SLEEP_SEC)
            continue
        await ensure_universe()
        insts = _active_insts()
        if not insts:
            await asyncio.sleep(_IDLE_SLEEP_SEC)
            continue
        key = _active_key()
        if _SDK_AVAILABLE:
            try:
                await _stream_session(insts, key)
                continue
            except Exception as exc:  # noqa: BLE001 — degrade to REST for this window
                log.warning("MM orderbook gRPC session failed (%r) — REST fallback", exc)
        try:
            await _rest_window(insts, key)
        except Exception as exc:  # noqa: BLE001 — self-heal
            log.warning("MM orderbook poll cycle failed: %s", exc)
            await asyncio.sleep(5.0)
