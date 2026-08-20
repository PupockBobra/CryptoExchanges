"""
Order-book snapshot collector for the MM-presence estimator (SPB perps).

Keeps a live book per instrument via Finam's gRPC stream and copies it to the DB
once every ``SAMPLE_SEC`` seconds on the epoch-aligned grid, top ``STORE_DEPTH``
levels per side.  REST polling is the fallback when the SDK is missing or the
gRPC session dies.

Why a stream plus a timer, rather than polling every 5 s: a REST sweep of 15
instruments costs ~8 s at the venue's proven throttle (0.5 s between calls, never
parallel — parallel bursts get 429'd), so a 5-second grid is simply not reachable
over REST.  The stream carries 4–17 messages/s per instrument at no rate-limit
cost, and the timer decides when a snapshot is taken.

Two things this file is careful about, because both would corrupt the metric the
page reports rather than merely degrade it:

* **A frozen feed must never look like a standing quote.**  Persistence is
  literally "the same size kept showing up", so a dead stream — book intact, no
  updates — would score a perfect 1.0.  A grid point is stored only while the
  session is known alive AND the instrument's book was refreshed within
  ``STALE_AFTER_SEC``; otherwise it is counted as a miss.
* **Misses are counted, not ignored.**  The share of skipped grid points is
  written to ``ob_capture_session`` and shown next to every estimate: a
  persistence of 0.9 over a window that lost a third of its snapshots is not the
  same statement as one over a complete window.
"""

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.timescale import (
    insert_ob_levels,
    open_capture_session,
    update_capture_session,
)
from app.mmdetect.config import MMD_TICKERS, SAMPLE_SEC, STALE_AFTER_SEC, STORE_DEPTH
from app.spb.config import FINAM_MIC
from app.spb.fetcher import FinamClient
from app.spb.orderbook import _dec, parse_levels
from app.spb.spread_etl import _is_trading_now

try:  # official gRPC SDK; without it the REST fallback below still works
    from finam_trade_api import AsyncFinamClient
    from finam_trade_api.market_data import StreamOrderBook, SubscribeOrderBookRequest
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — dev env without finam-sdk
    _SDK_AVAILABLE = False

log = logging.getLogger(__name__)

_STREAM_RETRY_SEC = 3.0
_STREAM_SILENCE_SEC = 30.0        # no message from ANY stream → channel presumed dead
_STREAM_CONNECT_TIMEOUT_SEC = 15.0
_STREAM_CLOSE_TIMEOUT_SEC = 10.0
_REST_THROTTLE_SEC = 0.5          # never parallel — the venue 429s bursts
_FLUSH_ROWS = 2_000               # write in batches, not per grid point
_FLUSH_SEC = 30.0

# ticker → {"bids": [{price,size}], "asks": [...], "updated": monotonic}
_books: dict[str, dict] = {}
_last_stream_msg: float = 0.0
_session_ids: dict[str, int] = {}
_counts: dict[str, list[int]] = {}      # ticker → [n_snapshots, n_missed]


def _fresh_state() -> None:
    global _books, _counts
    _books = {t: {"bids": [], "asks": [], "updated": 0.0} for t in MMD_TICKERS}
    _counts = {t: [0, 0] for t in MMD_TICKERS}


def _next_grid_point(now: datetime) -> datetime:
    """Next sampling instant strictly after ``now``, on the epoch-aligned
    ``SAMPLE_SEC`` grid — so a restart (or a second host) lands on the same
    instants and the dedup key actually dedups."""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % SAMPLE_SEC + SAMPLE_SEC, tz=timezone.utc)


def _rows_for(ticker: str, ts: datetime, book: dict) -> list[tuple]:
    rows = []
    for side, levels in (("bid", book["bids"]), ("ask", book["asks"])):
        for idx, lvl in enumerate(levels[:STORE_DEPTH]):
            if lvl["size"] <= 0:
                continue      # a zero-size level is a removed level, not a quote
            rows.append((ts, ticker, side, idx, float(lvl["price"]), float(lvl["size"])))
    return rows


# ── gRPC streaming (primary) ─────────────────────────────────────────────────

async def _consume_stream(client, ticker: str) -> None:
    """Apply the server's snapshot, then its deltas, into ``_books[ticker]``.
    Book state is local so every resubscribe restarts from a clean snapshot."""
    global _last_stream_msg
    remove = StreamOrderBook.Row.Action.ACTION_REMOVE
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    async for resp in client.market_data.SubscribeOrderBook(
        SubscribeOrderBookRequest(symbol=f"{ticker}@{FINAM_MIC}")
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
        _books[ticker] = {
            "bids": [{"price": p, "size": s} for p, s in sorted(bids.items(), reverse=True)],
            "asks": [{"price": p, "size": s} for p, s in sorted(asks.items())],
            "updated": time.monotonic(),
        }


async def _stream_ticker(client, ticker: str) -> None:
    while True:
        try:
            await _consume_stream(client, ticker)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — resubscribe, keep the loop alive
            log.warning("mmdetect stream %s dropped: %s", ticker, exc)
        await asyncio.sleep(_STREAM_RETRY_SEC)


async def _stream_session() -> None:
    """One gRPC session: 15 concurrent subscriptions plus the sampler.  Returns
    when trading hours end; raises when the channel dies so the caller can fall
    back to REST.  Every phase is time-bounded — the SDK has no deadlines and a
    broken channel can otherwise hang in connect forever."""
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
        _last_stream_msg = time.monotonic()      # grace period for first snapshots
        tasks = [asyncio.create_task(_stream_ticker(client, t)) for t in MMD_TICKERS]
        await _sample_loop(alive=lambda: time.monotonic() - _last_stream_msg <= _STREAM_SILENCE_SEC)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                   _STREAM_CLOSE_TIMEOUT_SEC)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.__aexit__(None, None, None), _STREAM_CLOSE_TIMEOUT_SEC)


# ── REST polling (fallback) ──────────────────────────────────────────────────

async def _rest_refresher(client: FinamClient, stop: asyncio.Event) -> None:
    """Sweep the instruments sequentially for as long as the fallback runs.  One
    sweep is ~8 s, so the 5-second grid degrades to "whatever the sweep managed"
    — the sampler marks the rest as misses, which is the honest record."""
    while not stop.is_set():
        for ticker in MMD_TICKERS:
            if stop.is_set():
                return
            try:
                # retries=2: a single attempt returns spurious 404s under load
                # (observed on COHR/COIN/UBER), and a phantom miss is worse here
                # than a slightly slower sweep — it biases the presence metric.
                ob = await client.fetch_orderbook(ticker, retries=2)
                bids, asks = parse_levels(ob)
                _books[ticker] = {"bids": bids, "asks": asks, "updated": time.monotonic()}
            except Exception as exc:  # noqa: BLE001 — leave the book stale → miss
                log.debug("mmdetect REST %s failed: %s", ticker, exc)
            await asyncio.sleep(_REST_THROTTLE_SEC)


async def _rest_session() -> None:
    stop = asyncio.Event()
    async with FinamClient(settings.finam_api_token) as client:
        refresher = asyncio.create_task(_rest_refresher(client, stop))
        try:
            await _sample_loop(alive=lambda: True)
        finally:
            stop.set()
            refresher.cancel()
            with contextlib.suppress(Exception):
                await refresher


# ── the sampler ──────────────────────────────────────────────────────────────

async def _sample_loop(alive) -> None:
    """Copy every live book to the DB on each grid point, until the session ends
    or trading hours close.  ``alive`` reports whether the underlying feed is
    still delivering — when it is not, grid points are counted as misses instead
    of storing a frozen book that would fake perfect persistence."""
    buf: list[tuple] = []
    last_flush = time.monotonic()
    while True:
        target = _next_grid_point(datetime.now(timezone.utc))
        while True:
            remaining = (target - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 1.0))
        if not _is_trading_now(target):
            await _flush(buf, force=True)
            return
        feed_ok = alive()
        now_mono = time.monotonic()
        for ticker in MMD_TICKERS:
            book = _books.get(ticker) or {}
            fresh = (
                feed_ok
                and book.get("updated", 0.0) > 0
                and now_mono - book["updated"] <= STALE_AFTER_SEC
                and (book.get("bids") or book.get("asks"))
            )
            if not fresh:
                _counts[ticker][1] += 1
                continue
            rows = _rows_for(ticker, target, book)
            if not rows:
                _counts[ticker][1] += 1
                continue
            buf.extend(rows)
            _counts[ticker][0] += 1
        if len(buf) >= _FLUSH_ROWS or time.monotonic() - last_flush >= _FLUSH_SEC:
            await _flush(buf)
            last_flush = time.monotonic()
        if not feed_ok:
            await _flush(buf, force=True)
            raise RuntimeError(
                f"no stream messages for {_STREAM_SILENCE_SEC:.0f}s — channel presumed dead"
            )


async def _flush(buf: list[tuple], force: bool = False) -> None:
    """Write the buffered rows and refresh the per-instrument session counters."""
    if buf:
        try:
            await insert_ob_levels(buf)
        except Exception as exc:  # noqa: BLE001 — keep collecting, drop this batch
            log.warning("mmdetect: insert of %d rows failed: %s", len(buf), exc)
        buf.clear()
    for ticker, (n_ok, n_miss) in _counts.items():
        sid = _session_ids.get(ticker)
        if sid is not None:
            with contextlib.suppress(Exception):
                await update_capture_session(sid, n_ok, n_miss, closed=force)


# ── entry point (spawned from main.py lifespan) ──────────────────────────────

async def mmdetect_collector_loop() -> None:
    """Capture order-book snapshots during SPB trading hours, forever.

    Unlike the display feed this is NOT lazy: the whole point is a continuous
    record, and there is nothing to analyse later for the minutes nobody had the
    page open.  Off-hours it sleeps — SPB's book is not quoted then, and storing
    a frozen book would only manufacture persistence.
    """
    if not settings.finam_api_token:
        log.info("mmdetect collector disabled (no Finam token)")
        return
    if not MMD_TICKERS:
        log.warning("mmdetect collector: empty ticker list, nothing to capture")
        return
    log.info("mmdetect collector started (%d tickers, %ds grid, %d levels/side, grpc=%s)",
             len(MMD_TICKERS), SAMPLE_SEC, STORE_DEPTH, _SDK_AVAILABLE)
    while True:
        if not _is_trading_now():
            await asyncio.sleep(60)
            continue
        _fresh_state()
        try:
            for ticker in MMD_TICKERS:
                _session_ids[ticker] = await open_capture_session(ticker, SAMPLE_SEC)
        except Exception as exc:  # noqa: BLE001 — capture is still worth doing
            log.warning("mmdetect: could not open capture sessions: %s", exc)
            _session_ids.clear()
        started = datetime.now(timezone.utc)
        try:
            if _SDK_AVAILABLE:
                try:
                    await _stream_session()
                    continue                      # trading hours ended
                except Exception as exc:  # noqa: BLE001 — degrade to REST
                    log.warning("mmdetect gRPC session failed (%r) — REST fallback", exc)
            await _rest_session()
        except Exception as exc:  # noqa: BLE001 — self-heal
            log.warning("mmdetect collector cycle failed: %s", exc)
            await asyncio.sleep(5.0)
        finally:
            took = datetime.now(timezone.utc) - started
            total_ok = sum(c[0] for c in _counts.values())
            total_miss = sum(c[1] for c in _counts.values())
            log.info("mmdetect: session over (%s), %d snapshots stored, %d missed (%.1f%%)",
                     str(took).split(".")[0], total_ok, total_miss,
                     100.0 * total_miss / max(1, total_ok + total_miss))


def _grid_points_between(a: datetime, b: datetime) -> int:
    """Grid points the collector *should* have taken in [a, b) — used by the API
    to turn stored-snapshot counts into a coverage ratio."""
    return max(0, int((b - a) / timedelta(seconds=SAMPLE_SEC)))
