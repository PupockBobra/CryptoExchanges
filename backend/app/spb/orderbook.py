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
import contextlib
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.moex.config import MOEX_CRYPTO_FUTURES, MOEX_FORTS_MIC
from app.moex.fetcher import resolve_front_secids
from app.spb.config import FINAM_MIC, SPB_GROUPS, SPB_LOTS, SPB_NAMES, SPB_TICKERS
from app.spb.fetcher import FinamClient, num_value

try:  # official gRPC SDK; without it the REST fallback below still works
    from finam_trade_api import AsyncFinamClient
    from finam_trade_api.market_data import StreamOrderBook, SubscribeOrderBookRequest
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - dev env without finam-sdk
    _SDK_AVAILABLE = False

log = logging.getLogger(__name__)

_DEPTH = 12                 # levels served to the frontend per side (the cache
                            # keeps FULL depth — the live spread math needs it)
_ACTIVE_WINDOW_SEC = 30.0  # keep the feed running this long after the last read
_IDLE_SLEEP_SEC = 3.0      # nap while nobody is watching
_STREAM_RETRY_SEC = 3.0    # pause before resubscribing a dropped stream
# The Finam gRPC channel can die *silently*: streams stay open but stop
# delivering deltas (observed live — snapshot arrives, then nothing, no error
# raised).  If no stream message lands for this long, the session is presumed
# dead and torn down so the window falls back to REST polling.
_STREAM_SILENCE_SEC = 30.0
# The SDK has no deadlines: connecting/closing a broken channel can hang forever
# (observed live — the feed loop froze inside __aenter__ with no log output).
# Every session phase is bounded by these.
_STREAM_CONNECT_TIMEOUT_SEC = 15.0
_STREAM_CLOSE_TIMEOUT_SEC = 10.0

# REST fallback tuning (matches the proven SPB ETL)
_THROTTLE_SEC = 0.5        # pause between individual Finam REST calls
_CYCLE_PAUSE_SEC = 2.0     # pause between full REST refresh cycles

_last_access: float = 0.0
_last_stream_msg: float = 0.0   # monotonic time of the last gRPC stream message
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

# MOEX crypto-futures live book, shown to the right of the 5 crypto cards.  Keyed
# by the SPB crypto ticker (same card).  Streamed via Finam under MIC RTSX; the
# front-month SECID is resolved daily (contracts roll monthly).
_MOEX_TICKERS: list[str] = list(MOEX_CRYPTO_FUTURES.keys())
_moex_cache: dict[str, dict] = {t: _placeholder(t) for t in _MOEX_TICKERS}
_moex_secids: dict[str, str] = {}       # SPB crypto ticker → front-month SECID
_moex_secids_day = None


def note_access() -> None:
    """Mark the cache as actively read — wakes / sustains the background feed."""
    global _last_access
    _last_access = time.monotonic()


def get_cached_orderbooks() -> list[dict]:
    """Current cached books in display order, sides cut to display depth.
    Renews the active window."""
    note_access()
    return [
        {**_cache[t], "bids": _cache[t]["bids"][:_DEPTH], "asks": _cache[t]["asks"][:_DEPTH]}
        for t in SPB_TICKERS
    ]


def get_cached_moex_orderbooks() -> list[dict]:
    """Current cached MOEX crypto-futures books (5), keyed by the SPB crypto
    ticker so the frontend overlays them on the same card.  Renews the window."""
    note_access()
    return [
        {**_moex_cache[t], "bids": _moex_cache[t]["bids"][:_DEPTH], "asks": _moex_cache[t]["asks"][:_DEPTH]}
        for t in _MOEX_TICKERS
    ]


async def _ensure_moex_secids() -> None:
    """Resolve the MOEX front-month SECID per crypto ticker once per UTC day.
    Blocking ISS call runs in a thread; failure keeps the last known map."""
    global _moex_secids, _moex_secids_day
    today = datetime.now(timezone.utc).date()
    if _moex_secids and _moex_secids_day == today:
        return
    assetcodes = [ac for ac, _lot in MOEX_CRYPTO_FUTURES.values()]
    loop = asyncio.get_event_loop()
    try:  # usdrub only feeds the (unused-here) lot calc → pass 1.0
        resolved = await loop.run_in_executor(None, resolve_front_secids, assetcodes, 1.0)
    except Exception as exc:  # noqa: BLE001 — keep last map
        log.warning("MOEX orderbook front-month resolve failed: %s", exc)
        return
    m = {t: resolved.get(ac, (None, None))[0]
         for t, (ac, _lot) in MOEX_CRYPTO_FUTURES.items()}
    m = {t: s for t, s in m.items() if s}
    if m:
        _moex_secids = m
        _moex_secids_day = today


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
        "bids":   [{"price": p, "size": s} for p, s in sorted(bids.items(), reverse=True)],
        "asks":   [{"price": p, "size": s} for p, s in sorted(asks.items())],
        "error":  None,
        "ts":     datetime.now(timezone.utc).isoformat(),
    }


async def _consume_stream(client, symbol: str, cache: dict, key: str) -> None:
    """One subscription: apply the snapshot, then deltas, re-rendering ``cache[key]``
    on every message.  Book state lives here so every (re)subscribe starts clean
    from the server's fresh snapshot.  ``symbol`` is the full Finam symbol
    (``TICKER@RUSX`` for SPB, ``SECID@RTSX`` for MOEX)."""
    global _last_stream_msg
    remove = StreamOrderBook.Row.Action.ACTION_REMOVE
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
                else:  # ADD / UPDATE — upsert the price level
                    side[price] = _dec(row.buy_size if is_bid else row.sell_size)
        cache[key] = _render(key, bids, asks)


async def _stream_ticker(client, symbol: str, cache: dict, key: str) -> None:
    """Keep one subscription alive for the whole session; if the server closes
    the stream (or it errors), resubscribe after a short pause.  Previous levels
    stay in the cache so the card never blanks."""
    while True:
        try:
            await _consume_stream(client, symbol, cache, key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep last book, resubscribe
            prev = cache.get(key) or _placeholder(key)
            cache[key] = {**prev, "error": str(exc)}
            log.warning("orderbook stream %s dropped: %s", symbol, exc)
        await asyncio.sleep(_STREAM_RETRY_SEC)


async def _stream_session() -> None:
    """One gRPC session per active window: 25 concurrent server-streams on a
    single channel.  The SDK refreshes the JWT in the background; the channel
    and all subscriptions are torn down when the viewer leaves — or when the
    silence watchdog trips (dead channel that raises no error).  Every phase
    (connect / run / teardown) is time-bounded so a broken channel can never
    freeze the feed loop."""
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
        _last_stream_msg = time.monotonic()   # grace period for the first snapshots
        await _ensure_moex_secids()
        tasks = [
            asyncio.create_task(_stream_ticker(client, f"{t}@{FINAM_MIC}", _cache, t))
            for t in SPB_TICKERS
        ]
        tasks += [
            asyncio.create_task(
                _stream_ticker(client, f"{secid}@{MOEX_FORTS_MIC}", _moex_cache, t)
            )
            for t, secid in _moex_secids.items()
        ]
        while _active():
            await asyncio.sleep(1.0)
            if time.monotonic() - _last_stream_msg > _STREAM_SILENCE_SEC:
                raise RuntimeError(
                    f"no stream messages for {_STREAM_SILENCE_SEC:.0f}s — channel presumed dead"
                )
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), _STREAM_CLOSE_TIMEOUT_SEC
            )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.__aexit__(None, None, None), _STREAM_CLOSE_TIMEOUT_SEC)


# --------------------------------------------------------------------------
# Fallback feed: sequential REST polling (~15 s per full cycle)
# --------------------------------------------------------------------------

def parse_levels(ob: dict) -> tuple[list[dict], list[dict]]:
    """Parse a Finam orderbook payload into full-depth (bids, asks), sorted best
    first.  Each level is ``{"price", "size"}`` (USD / contracts)."""
    bids, asks = [], []
    for r in ob.get("rows", []):
        price = num_value(r, "price")
        if r.get("buy_size") is not None:
            bids.append({"price": price, "size": num_value(r, "buy_size")})
        elif r.get("sell_size") is not None:
            asks.append({"price": price, "size": num_value(r, "sell_size")})
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])
    return bids, asks


def _vwap_to_notional(levels: list[dict], lot: float, usdrub: float,
                      target_rub: float) -> float | None:
    """
    Walk ``levels`` (best first) accumulating notional until ``target_rub`` roubles
    are filled; return the volume-weighted average price (USD) of the filled
    quantity.  The last level is filled partially so exactly ``target_rub`` is met.
    ``None`` if the levels don't hold that much notional.
    """
    filled_rub = 0.0
    qty_sum = 0.0        # contracts filled
    pv_sum = 0.0         # Σ price·qty
    for lvl in levels:
        price, size = lvl["price"], lvl["size"]
        level_rub = price * size * lot * usdrub
        if level_rub <= 0:
            continue
        if filled_rub + level_rub >= target_rub:
            frac = (target_rub - filled_rub) / level_rub    # 0..1 of this level
            qty = size * frac
            pv_sum += price * qty
            qty_sum += qty
            return pv_sum / qty_sum if qty_sum > 0 else None
        filled_rub += level_rub
        pv_sum += price * size
        qty_sum += size
    return None          # not enough depth


def _avg_fill_prices(bids: list[dict], asks: list[dict], lot: float,
                     usdrub: float, target_rub: float):
    """
    VWAP ask/bid fill prices (USD) to trade ``target_rub`` roubles on *each* side
    (``target_rub`` on the ask, ``target_rub`` on the bid), walking the book.
    Returns ``(p_ask, p_bid)`` or ``(None, None)`` if either side lacks that
    depth.  ``lot``/``usdrub`` only size the rouble fill.
    """
    p_ask = _vwap_to_notional(asks, lot, usdrub, target_rub)
    p_bid = _vwap_to_notional(bids, lot, usdrub, target_rub)
    if p_ask is None or p_bid is None:
        return None, None
    return p_ask, p_bid


def avg_spread_on_volume(bids: list[dict], asks: list[dict], lot: float,
                         usdrub: float, target_rub: float) -> float | None:
    """
    AVG_SPREAD in **USD**: the raw VWAP price gap ``P_aver_ask - P_aver_bid`` to
    trade ``target_rub`` roubles on *each* side of the book (walking it).  NOT
    multiplied by the lot and NOT converted to roubles — it is the dollar spread
    per unit of the quoted price, so it is directly comparable across venues with
    different contract sizes (SPB vs MOEX).  ``lot``/``usdrub`` only size the
    rouble fill depth.  ``None`` if either side lacks ``target_rub`` of depth.
    """
    p_ask, p_bid = _avg_fill_prices(bids, asks, lot, usdrub, target_rub)
    if p_ask is None:
        return None
    return p_ask - p_bid


def spread_pct_on_volume(bids: list[dict], asks: list[dict], lot: float,
                         usdrub: float, target_rub: float) -> float | None:
    """
    AVG_SPREAD expressed as a percentage of the top-of-book middle point —
    ``(P_aver_ask - P_aver_bid) / P_mid * 100`` with ``P_mid = (best_bid +
    best_ask) / 2`` (the mid of the current best quotes, NOT the averaged fill
    mid).  Unit-free: the ratio cancels ``lot`` and the fx rate (they only size
    the fill), so no USD→RUB conversion is needed downstream.  ``None`` if either
    side lacks ``target_rub`` of depth.
    """
    p_ask, p_bid = _avg_fill_prices(bids, asks, lot, usdrub, target_rub)
    if p_ask is None:
        return None
    mid = (bids[0]["price"] + asks[0]["price"]) / 2.0
    return (p_ask - p_bid) / mid * 100.0 if mid > 0 else None


def compute_live_spreads(usdrub: float) -> list[dict]:
    """
    Instantaneous AVG_SPREAD per ticker from the *current* cached books —
    tick-by-tick fresh while the page is open (gRPC stream).  Values are
    per-contract USD for 1 млн / 10 млн ₽ of executed volume; tickers whose
    book is still empty are omitted.  Does NOT renew the active window — only an
    API read may do that, so nothing can keep the feed alive by itself.
    """
    out = []
    for ticker in SPB_TICKERS:
        book = _cache[ticker]
        if not book["bids"] and not book["asks"]:
            continue
        lot = SPB_LOTS.get(ticker, 1.0)
        out.append({
            "ticker":         ticker,
            "spread_1m_usd":  avg_spread_on_volume(book["bids"], book["asks"], lot, usdrub, 1_000_000.0),
            "spread_10m_usd": avg_spread_on_volume(book["bids"], book["asks"], lot, usdrub, 10_000_000.0),
        })
    return out


def _parse(ticker: str, ob: dict) -> dict:
    bids, asks = parse_levels(ob)
    return {
        "ticker": ticker,
        "name":   SPB_NAMES.get(ticker, ticker),
        "group":  SPB_GROUPS.get(ticker, "US Market"),
        "bids":   bids,
        "asks":   asks,
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

        # MOEX crypto-futures books (RTSX), same sequential throttle.
        await _ensure_moex_secids()
        for ticker, secid in _moex_secids.items():
            if not _active():
                return
            try:
                ob = await client.fetch_orderbook(secid, mic=MOEX_FORTS_MIC, retries=1)
                _moex_cache[ticker] = _parse(ticker, ob)
            except Exception as exc:  # noqa: BLE001 — keep last book, record error
                prev = _moex_cache.get(ticker) or _placeholder(ticker)
                _moex_cache[ticker] = {**prev, "error": str(exc)}
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
                log.warning("SPB orderbook gRPC session failed (%r) — falling back to REST", exc)
        try:
            await _rest_poll_window()
        except Exception as exc:  # noqa: BLE001 — self-heal on dropped pool / stale JWT
            log.warning("SPB orderbook poll cycle failed: %s", exc)
            await asyncio.sleep(5.0)
