import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.timescale import insert_tick
from app.arbitrage.detector import on_tick
from app.redis_client import get_redis

log = logging.getLogger(__name__)


def redis_channel(symbol: str) -> str:
    """BTC/USDT → prices:BTC_USDT   XAU/USDT:USDT → prices:XAU_USDT_USDT"""
    return f"prices:{symbol.replace('/', '_').replace(':', '_')}"


def _is_not_supported(exc: Exception) -> bool:
    """True when ccxt raises NotSupported (runtime check, avoids fragile imports)."""
    return "NotSupported" in type(exc).__name__ or "not support" in str(exc).lower()


# After this many consecutive watch_tickers failures, stop spinning the
# per-symbol fallback and surface the error to run() so it marks the collector
# disconnected and reconnects with backoff (a hung socket otherwise loops forever).
_MAX_CONSECUTIVE_FAILURES = 5


class BaseCollector(ABC):
    exchange_id: str
    reconnect_delay: float = 5.0

    def __init__(
        self,
        symbols: list[str],
        db_aliases: dict[str, dict[str, str]] | None = None,
    ):
        self.symbols      = symbols
        self.spot_symbols = [s for s in symbols if ":" not in s]
        self.perp_symbols = [s for s in symbols if ":" in s]
        # DB-sourced aliases: { canonical: { exchange_id: exchange_sym } }
        self._db_aliases  = db_aliases or {}

        # ── Runtime stats ────────────────────────────────────────────────────
        self._status:         str              = "connecting"
        self._ticks_total:    int              = 0
        self._bytes_in:       int              = 0          # approx raw bytes
        self._reconnects:     int              = 0
        self._tick_window:    deque[float]     = deque()   # timestamps (60 s window)
        self._last_tick_ts:   datetime | None  = None
        self._symbols_active: list[str]        = []
        self._started_at:     datetime | None  = None

    # ── Public lifecycle ─────────────────────────────────────────────────────

    def _resolve_symbols(self, canonical_symbols: list[str]) -> dict[str, str]:
        """
        Build {exchange_sym: canonical_sym} for this collector's exchange.

        Resolution order (first match wins):
          1. DB-stored aliases (set via the instruments UI)
               - explicit null  → skip this symbol on this exchange entirely
               - string value   → use as the exchange symbol
          2. SYMBOL_ALIASES env-var overrides
          3. Canonical symbol unchanged (pass-through)
        """
        env_aliases = settings.symbol_aliases_dict
        result: dict[str, str] = {}
        for sym in canonical_symbols:
            db_sym_aliases = self._db_aliases.get(sym, {})
            if self.exchange_id in db_sym_aliases:
                alias = db_sym_aliases[self.exchange_id]
                if alias is None:
                    # Explicit null → this exchange doesn't list the instrument
                    continue
                exchange_sym = alias
            else:
                exchange_sym = (
                    env_aliases.get(sym, {}).get(self.exchange_id)
                    or sym
                )
            existing = result.get(exchange_sym)
            if existing is not None and existing != sym:
                # Two canonical symbols resolve to the same exchange symbol —
                # downstream tick dispatch can only route to one of them.
                log.warning(
                    "[%s] alias collision: %r and %r both map to %r — keeping %r",
                    self.exchange_id, existing, sym, exchange_sym, existing,
                )
                continue
            result[exchange_sym] = sym
        return result

    async def run(self):
        self._started_at = datetime.now(timezone.utc)
        self._status     = "connecting"
        stats_task = asyncio.create_task(self._stats_loop(), name=f"stats-{self.exchange_id}")
        try:
            while True:
                try:
                    log.info("[%s] connecting…", self.exchange_id)
                    await self._connect()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._status = "disconnected"
                    self._reconnects += 1
                    log.warning(
                        "[%s] disconnected: %s — retry in %ss",
                        self.exchange_id, e, self.reconnect_delay,
                    )
                    await asyncio.sleep(self.reconnect_delay)
                    self._status = "connecting"
        except asyncio.CancelledError:
            self._status = "disconnected"
            raise
        finally:
            stats_task.cancel()
            await asyncio.gather(stats_task, return_exceptions=True)

    @abstractmethod
    async def _connect(self):
        """Open exchange WebSocket(s) and stream tickers until disconnected."""

    # ── Streaming helpers ────────────────────────────────────────────────────

    async def _stream_tickers(self, exchange: Any, symbols: list[str]):
        """
        Stream tickers for the given canonical symbols.

        1. Tries watch_tickers (batch) first — most efficient.
        2. If the exchange raises NotSupported for this market type, switches
           permanently to parallel per-symbol watch_ticker tasks.

        All ticks published under the canonical symbol regardless of aliases.
        """
        sym_map = self._resolve_symbols(symbols)
        exchange_syms = list(sym_map.keys())

        try:
            await exchange.load_markets()
            valid_ex = [s for s in exchange_syms if s in exchange.markets]
            if not valid_ex:
                log.info(
                    "[%s] none of %s available in this market",
                    self.exchange_id, exchange_syms,
                )
                return
            if len(valid_ex) < len(exchange_syms):
                skipped = {sym_map[s] for s in set(exchange_syms) - set(valid_ex)}
                log.info("[%s] skipping unavailable symbols: %s", self.exchange_id, skipped)
        except Exception:
            valid_ex = exchange_syms

        sym_map = {s: sym_map[s] for s in valid_ex}
        # Accumulate active symbols across spot + perp market streams
        new_active = list(sym_map.values())
        combined = list(dict.fromkeys(self._symbols_active + new_active))
        self._symbols_active = combined
        self._status = "connected"

        consecutive_failures = 0
        while True:
            try:
                tickers = await exchange.watch_tickers(valid_ex)
            except Exception as exc:
                if _is_not_supported(exc):
                    log.info(
                        "[%s] watch_tickers not supported, using concurrent watch_ticker",
                        self.exchange_id,
                    )
                    await asyncio.gather(*(
                        self._single_ticker_loop(exchange, ex_sym, sym_map[ex_sym])
                        for ex_sym in valid_ex
                    ))
                    return
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    # Socket is likely dead, not just hiccuping — raise so run()
                    # reconnects with backoff instead of spinning here forever.
                    log.warning(
                        "[%s] watch_tickers failed %d× in a row, forcing reconnect: %s",
                        self.exchange_id, consecutive_failures, exc,
                    )
                    raise
                # Transient error — sequential fallback for one round, then retry
                for ex_sym in valid_ex:
                    try:
                        ticker = await exchange.watch_ticker(ex_sym)
                        await self._handle_ticker(sym_map[ex_sym], ticker)
                    except Exception as err:
                        log.debug("[%s] watch_ticker %s: %s", self.exchange_id, ex_sym, err)
                continue

            consecutive_failures = 0
            for ex_sym, ticker in tickers.items():
                if ex_sym in sym_map:
                    await self._handle_ticker(sym_map[ex_sym], ticker)

    async def _single_ticker_loop(self, exchange: Any, ex_sym: str, canonical: str):
        """Persistent watch_ticker loop for one symbol (fallback path)."""
        while True:
            try:
                ticker = await exchange.watch_ticker(ex_sym)
                await self._handle_ticker(canonical, ticker)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.debug(
                    "[%s] watch_ticker %s: %s — retrying in 2 s",
                    self.exchange_id, ex_sym, err,
                )
                await asyncio.sleep(2)

    async def _handle_ticker(self, symbol: str, ticker: dict):
        bid  = float(ticker.get("bid")  or 0)
        ask  = float(ticker.get("ask")  or 0)
        last = float(ticker.get("last") or 0)

        # Some exchange WS streams (e.g. MEXC spot) omit 'last'.  Synthesize
        # from bid+ask mid-price only — falling back to a one-sided bid/ask
        # was producing false arbitrage signals when the missing side moved.
        if not last:
            if bid and ask:
                last = (bid + ask) / 2
            else:
                return  # not enough info to derive a fair last price

        # If only one side of the book is present, mirror to the other side so
        # downstream consumers always see a non-zero bid/ask, but never use a
        # synthesized side as a trade price for arbitrage decisions.
        if not bid:
            bid = last
        if not ask:
            ask = last

        volume = float(ticker.get("quoteVolume") or 0)
        await self._publish(symbol, bid, ask, last, volume)

    async def _publish(
        self,
        symbol: str,
        bid: float,
        ask: float,
        last: float,
        volume: float = 0,
    ):
        ts = datetime.now(timezone.utc)
        channel = redis_channel(symbol)
        payload = json.dumps({
            "exchange": self.exchange_id,
            "symbol":   symbol,
            "bid":      bid,
            "ask":      ask,
            "last":     last,
            "volume":   volume,
            "ts":       ts.isoformat(),
        })

        # ── Update stats counters ────────────────────────────────────────────
        now_ts = ts.timestamp()
        self._ticks_total  += 1
        self._bytes_in     += len(payload)
        self._last_tick_ts  = ts
        self._tick_window.append(now_ts)
        # Purge entries older than 60 s
        while self._tick_window and self._tick_window[0] < now_ts - 60:
            self._tick_window.popleft()

        r = await get_redis()
        await r.publish(channel, payload)
        await insert_tick(self.exchange_id, symbol, bid, ask, last)
        await on_tick(self.exchange_id, symbol, bid, ask, last)

    # ── Stats publishing ─────────────────────────────────────────────────────

    async def _stats_loop(self):
        """Publish stats to Redis hash + channel every 2 seconds."""
        while True:
            try:
                await asyncio.sleep(2)
                await self._emit_stats()
            except asyncio.CancelledError:
                await self._emit_stats()   # final snapshot before exit
                raise
            except Exception as e:
                log.debug("[%s] stats error: %s", self.exchange_id, e)

    async def _emit_stats(self):
        now = datetime.now(timezone.utc)
        stats = {
            "exchange":       self.exchange_id,
            "status":         self._status,
            "ticks_total":    self._ticks_total,
            "ticks_1m":       len(self._tick_window),
            "bytes_in":       self._bytes_in,
            "reconnects":     self._reconnects,
            "last_tick_ts":   self._last_tick_ts.isoformat() if self._last_tick_ts else "",
            "symbols_active": json.dumps(self._symbols_active),
            "started_at":     self._started_at.isoformat() if self._started_at else "",
            "updated_at":     now.isoformat(),
        }
        try:
            r = await get_redis()
            # Store in hash (for REST endpoint snapshot)
            await r.hset(f"exchange:stats:{self.exchange_id}", mapping=stats)
            # Publish for WebSocket live feed (deserialize lists first)
            pub = dict(stats)
            pub["symbols_active"] = self._symbols_active
            await r.publish(f"stats:{self.exchange_id}", json.dumps(pub))
        except Exception as e:
            log.debug("[%s] emit_stats error: %s", self.exchange_id, e)
