"""
Open Interest collector.

Exchange support:
  Binance     — history: openInterestValue = sumOpenInterestValue (USD) ✓
                live:    openInterestValue = None → calculate via ticker
  OKX         — history + live: openInterestValue = oiUsd ✓
  Bybit       — ccxt parse_open_interest always returns openInterest=None for linear
                even with markets loaded; extract from info['openInterest'] instead
                USD: daily prices (backfill) / ticker (live)
  Hyperliquid — info['openInterest'] + info['markPx'] → USD directly, no extra call
                no history API support
  MEXC        — fetch_open_interest() NotSupported via ccxt;
                use contract.mexc.com REST: holdVol × contractSize × lastPrice
                no history API support
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
import ccxt.async_support as ccxt_async

from app.config import settings
from app.db.timescale import (
    fetch_instruments,
    upsert_open_interest,
    get_pool,
)
from app.exchanges import EXCHANGE_CLS, make_exchange, CRYPTO_PERP_OVERRIDES

log = logging.getLogger(__name__)

POLL_INTERVAL = 1_800   # 30 minutes
STARTUP_DELAY = 180
_TIMEFRAME    = "1d"    # daily bars: limit=500 → 500 days, no gap at page boundary


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_ts(ms) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except Exception:
        return None


def _to_mexc_sym(ccxt_sym: str) -> str:
    """'BTC/USDT:USDT' → 'BTC_USDT'"""
    return ccxt_sym.split(":")[0].replace("/", "_")


def _extract_oi_from_result(result: dict, ex_id: str) -> tuple[float | None, float | None]:
    """
    Return (oi_contracts, oi_usdt) from a ccxt fetch_open_interest result.
    Falls back to raw info dict when ccxt normalization returns None.
    """
    oi     = _safe_float(result.get("openInterest"))
    oi_val = _safe_float(result.get("openInterestValue"))
    raw_info = result.get("info")
    # OKX daily returns info as a raw list ['ts', 'oiUsd', 'oiVol'] — guard against that
    info   = raw_info if isinstance(raw_info, dict) else {}

    if oi is None:
        oi = _safe_float(info.get("openInterest") or info.get("open_interest"))

    if oi_val is None and oi is not None and ex_id == "hyperliquid":
        price = _safe_float(info.get("markPx") or info.get("midPx") or info.get("oraclePx"))
        if price:
            oi_val = oi * price

    return oi, oi_val


async def _build_work_list() -> list[tuple[str, str, str]]:
    """[(exchange_id, exchange_symbol, canonical), ...] for all enabled perps."""
    instruments = await fetch_instruments(enabled_only=True)
    work: list[tuple[str, str, str]] = []
    for inst in instruments:
        canonical: str = inst["canonical"]
        # BTC/ETH/SOL are spot instruments (no ":") but their open interest is a
        # perpetual-futures metric — collect it via the perp symbols, stored
        # under the same canonical (matches the volume tabs' Crypto Perps group).
        perp_override = CRYPTO_PERP_OVERRIDES.get(canonical)
        if ":" not in canonical and perp_override is None:
            continue
        raw = inst["aliases"]
        aliases: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})
        for ex_id in settings.exchanges:
            if ex_id not in EXCHANGE_CLS:
                continue
            if perp_override is not None:
                ex_sym = perp_override.get(ex_id)
                if ex_sym is None:
                    continue
                work.append((ex_id, ex_sym, canonical))
                continue
            alias_val = aliases.get(ex_id)
            if alias_val is None and ex_id in aliases:
                continue
            ex_sym = alias_val or canonical
            work.append((ex_id, ex_sym, canonical))
    return work


async def _get_daily_prices(canonical: str, ex_id: str) -> dict:
    """
    Daily close prices from ohlcv_daily for price-based OI USD calculation.
    Prefers prices from `ex_id`, falls back to any exchange for missing dates.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (ts::date)
            ts::date AS d, close AS price
        FROM ohlcv_daily
        WHERE symbol = $1
          AND ts <  CURRENT_DATE + INTERVAL '1 day'
        ORDER BY ts::date,
                 CASE WHEN exchange = $2 THEN 0 ELSE 1 END,
                 ts DESC
        """,
        canonical, ex_id,
    )
    return {r["d"]: float(r["price"]) for r in rows}


# ── MEXC direct REST ──────────────────────────────────────────────────────────

_MEXC_BASE = "https://contract.mexc.com"

async def _fetch_mexc_contract_sizes() -> dict[str, float]:
    """One-time fetch of contractSize per MEXC symbol."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{_MEXC_BASE}/api/v1/contract/detail", timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
                return {
                    item["symbol"]: float(item["contractSize"])
                    for item in (data.get("data") or [])
                    if item.get("contractSize")
                }
    except Exception as e:
        log.warning("MEXC: failed to fetch contract sizes: %s", e)
        return {}


async def _poll_mexc(
    sym_pairs: list[tuple[str, str]],   # [(ccxt_sym, canonical), ...]
    contract_sizes: dict[str, float],
) -> None:
    if not sym_pairs:
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{_MEXC_BASE}/api/v1/contract/ticker", timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
        ticker_map = {item["symbol"]: item for item in (data.get("data") or [])}
        ts   = datetime.now(tz=timezone.utc)
        rows: list[tuple] = []
        for ccxt_sym, canonical in sym_pairs:
            msym = _to_mexc_sym(ccxt_sym)
            t    = ticker_map.get(msym)
            if not t:
                log.debug("MEXC: no ticker for %s (%s)", canonical, msym)
                continue
            hold_vol = _safe_float(t.get("holdVol"))
            price    = _safe_float(t.get("lastPrice") or t.get("fairPrice"))
            c_size   = contract_sizes.get(msym)
            if c_size is None:
                log.debug("MEXC: no contractSize for %s (%s) — skipping", canonical, msym)
                continue
            if hold_vol and price:
                oi_contracts = hold_vol * c_size
                oi_usdt      = oi_contracts * price
                rows.append((ts, "mexc", canonical, oi_contracts, oi_usdt))
        if rows:
            await upsert_open_interest(rows)
            log.debug("MEXC: OI poll: stored %d rows", len(rows))
    except Exception as e:
        log.warning("MEXC: OI poll error: %s", e)


# ── Backfill (ccxt exchanges, excl. MEXC + Hyperliquid) ───────────────────────

async def _backfill_symbol(ex_id: str, ex_sym: str, canonical: str) -> int:
    # Try the widest window the exchange accepts, falling back to shorter ones.
    # Exchanges cap how far back OI history goes and reject wider ranges with an
    # error (OKX 50030 "Illegal time range"; Binance rejects startTime older than
    # 30 days) — so a "too wide" window must fall through to a shorter one, never
    # give up. Worse, OKX returns its OLDEST slice (omitting recent days) when
    # `since` predates the available history, so we also reject any window whose
    # newest bar is stale and retry shorter. The UI shows ≤90 days.
    now = datetime.now(tz=timezone.utc)
    fallback_windows = [90, 30]
    stale_before = now - timedelta(days=3)
    last_idx = len(fallback_windows) - 1
    exchange = make_exchange(ex_id)
    raw_rows: list[tuple] = []
    try:
        last_err: Exception | None = None
        for i, days in enumerate(fallback_windows):
            # Floor to midnight UTC: a since with the current time-of-day excludes
            # the oldest available daily bar (e.g. Binance keeps 30 days but a
            # since of "now − 30d" at 09:00 drops that day's 00:00 bar). Flooring
            # stays within the exchange limit (Binance accepts up to ~30d23h).
            since_dt = (now - timedelta(days=days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            since_ms = int(since_dt.timestamp() * 1000)
            try:
                history = await exchange.fetch_open_interest_history(
                    ex_sym, _TIMEFRAME, since=since_ms, limit=500
                )
            except (ccxt_async.NotSupported, ccxt_async.BadSymbol):
                break  # exchange/symbol has no OI history at all
            except Exception as e:
                # Window too wide (OKX 50030, Binance startTime) or transient —
                # try a shorter window before giving up.
                last_err = e
                continue
            last_err = None
            parsed: list[tuple] = []
            newest: datetime | None = None
            for item in history:
                ts = _parse_ts(item.get("timestamp"))
                if ts is None:
                    continue
                oi, oi_val = _extract_oi_from_result(item, ex_id)
                parsed.append((ts, ex_id, canonical, oi, oi_val))
                if newest is None or ts > newest:
                    newest = ts
            # Keep the widest slice seen as a last-resort fallback.
            if len(parsed) > len(raw_rows):
                raw_rows = parsed
            # Reject an empty or stale window (OKX oldest-slice quirk) and try a
            # shorter one, unless this is already the shortest window.
            if (newest is None or newest < stale_before) and i != last_idx:
                continue
            raw_rows = parsed
            break  # success with recent data
        if last_err:
            log.warning("[%s] OI backfill gave up for %s after all windows: %s", ex_id, canonical, last_err)
    finally:
        try:
            await exchange.close()
        except Exception:
            pass

    if not raw_rows:
        return 0

    # Fill missing oi_usdt via daily close prices
    if any(r[4] is None and r[3] is not None for r in raw_rows):
        daily_prices = await _get_daily_prices(canonical, ex_id)
        rows: list[tuple] = []
        for ts, ex, sym, oi, oi_val in raw_rows:
            if oi_val is None and oi is not None:
                d     = ts.date()
                price = daily_prices.get(d) or daily_prices.get(d - timedelta(days=1))
                if price:
                    oi_val = oi * price
            rows.append((ts, ex, sym, oi, oi_val))
    else:
        rows = raw_rows

    rows = [r for r in rows if r[4] is not None]
    if not rows:
        return 0

    stored = await upsert_open_interest(rows)
    log.info("[%s] OI backfill: %d rows for %s", ex_id, stored, canonical)
    return stored


# ── Live poll (ccxt exchanges, excl. MEXC) ────────────────────────────────────

async def _poll_ccxt(work: list[tuple[str, str, str]]) -> None:
    by_exchange: dict[str, list[tuple[str, str]]] = {}
    for ex_id, ex_sym, canonical in work:
        by_exchange.setdefault(ex_id, []).append((ex_sym, canonical))

    for ex_id, sym_pairs in by_exchange.items():
        exchange = make_exchange(ex_id)
        try:
            rows: list[tuple] = []
            ts = datetime.now(tz=timezone.utc)

            if ex_id == "hyperliquid":
                # HIP3 markets (XYZ-* prefix) are not accessible via fetch_open_interest().
                # load_markets() embeds OI + markPx directly in exchange.markets[sym]['info'].
                await exchange.load_markets()
                for ex_sym, canonical in sym_pairs:
                    mkt = exchange.markets.get(ex_sym)
                    if mkt is None:
                        log.debug("[hyperliquid] market not loaded: %s", ex_sym)
                        continue
                    info = mkt.get("info") or {}
                    oi       = _safe_float(info.get("openInterest"))
                    mark_px  = _safe_float(info.get("markPx") or info.get("oraclePx"))
                    if oi is None or mark_px is None:
                        log.debug("[hyperliquid] missing OI or price for %s", ex_sym)
                        continue
                    oi_val = oi * mark_px
                    rows.append((ts, "hyperliquid", canonical, oi, oi_val))
            else:
                for ex_sym, canonical in sym_pairs:
                    try:
                        result = await exchange.fetch_open_interest(ex_sym)
                        oi, oi_val = _extract_oi_from_result(result, ex_id)
                        result_ts  = _parse_ts(result.get("timestamp")) or ts

                        if oi_val is None and oi is not None:
                            try:
                                ticker = await exchange.fetch_ticker(ex_sym)
                                price  = _safe_float(ticker.get("last") or ticker.get("close"))
                                if price:
                                    oi_val = oi * price
                            except Exception as e:
                                log.debug("[%s] ticker for OI calc failed (%s): %s", ex_id, canonical, e)

                        if oi_val is None:
                            await asyncio.sleep(0.2)
                            continue

                        rows.append((result_ts, ex_id, canonical, oi, float(oi_val)))
                        await asyncio.sleep(0.2)
                    except (ccxt_async.NotSupported, ccxt_async.BadSymbol):
                        log.debug("[%s] OI not supported for %s", ex_id, canonical)
                    except Exception as e:
                        log.warning("[%s] OI fetch error for %s: %s", ex_id, canonical, e)

            if rows:
                await upsert_open_interest(rows)
                log.debug("[%s] OI live: stored %d rows", ex_id, len(rows))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[%s] OI poll error: %s", ex_id, e)
        finally:
            try:
                await exchange.close()
            except Exception:
                pass


# ── Entry point ───────────────────────────────────────────────────────────────

async def oi_collector_loop() -> None:
    log.info("OI collector starting — waiting %ds…", STARTUP_DELAY)
    await asyncio.sleep(STARTUP_DELAY)

    # Contract sizes for MEXC (refreshed below when a tracked pair lacks one)
    contract_sizes = await _fetch_mexc_contract_sizes()

    sem = asyncio.Semaphore(2)
    # Pairs whose history has already been backfilled this process lifetime —
    # newly enabled instruments get one backfill pass when they first appear.
    backfilled: set[tuple[str, str, str]] = set()

    async def limited(ex_id: str, ex_sym: str, canonical: str) -> None:
        async with sem:
            try:
                await _backfill_symbol(ex_id, ex_sym, canonical)
            except Exception as e:
                log.warning("[%s] OI backfill failed for %s: %s", ex_id, canonical, e)

    while True:
        try:
            # Rebuild the work list every cycle so instruments enabled after
            # startup are picked up without a backend restart, and an initially
            # empty instruments table doesn't kill the collector for good.
            work = await _build_work_list()
            if not work:
                log.warning(
                    "OI collector: no enabled perp instruments — retrying in %ds",
                    POLL_INTERVAL,
                )
            else:
                # Split into MEXC (custom REST) and everything else (ccxt)
                mexc_pairs = [(ex_sym, canonical) for ex_id, ex_sym, canonical in work if ex_id == "mexc"]
                ccxt_work  = [(ex_id, ex_sym, canonical) for ex_id, ex_sym, canonical in work if ex_id != "mexc"]

                # Backfill history once per newly-seen pair (Binance/OKX/Bybit
                # only — MEXC and Hyperliquid expose no OI history API).
                backfill_work = [
                    w for w in ccxt_work
                    if w[0] not in ("mexc", "hyperliquid") and w not in backfilled
                ]
                if backfill_work:
                    await asyncio.gather(*[limited(*args) for args in backfill_work], return_exceptions=True)
                    backfilled.update(backfill_work)
                    log.info("OI backfill complete (%d pairs)", len(backfill_work))

                # Refresh MEXC contract sizes when any tracked pair is missing
                # one — covers newly listed contracts and a failed initial fetch
                # (one cheap HTTP call, only when actually needed).
                if mexc_pairs and any(_to_mexc_sym(s) not in contract_sizes for s, _ in mexc_pairs):
                    fresh = await _fetch_mexc_contract_sizes()
                    if fresh:
                        contract_sizes = fresh

                await asyncio.gather(
                    _poll_ccxt(ccxt_work),
                    _poll_mexc(mexc_pairs, contract_sizes),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("OI poll error: %s", e, exc_info=True)
        await asyncio.sleep(POLL_INTERVAL)
