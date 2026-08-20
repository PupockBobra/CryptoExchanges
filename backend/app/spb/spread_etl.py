"""
SPB order-book "spread on volume" (AVG_SPREAD) collector.

Samples the live order book of every SPB perp once per **wall-clock minute**
(sweep starts pinned to :00 of each minute) and writes the **plain average** of
those samples as one 15-minute chart point, for two depth targets: 1 000 000 ₽
and 10 000 000 ₽ *on each side* (that much on the bid and that much on the ask,
walking the book — see ``avg_spread_on_volume`` in ``orderbook.py``).

Wall-clock pinning matters (2026-07-15): the spread is highly volatile (BTC
swings ±80% within a minute), so two instances sampling at unsynchronized
moments produced ~10% different bucket averages.  With sweeps starting on the
shared epoch-aligned minute grid, every instance samples instrument *k* at the
same instants → the averages, and therefore the charts, agree across hosts.

Unlike the display poller in ``orderbook.py`` (lazy — runs only while the page is
open), this samples on every trading-hours minute so the history is continuous.
Order-book depth cannot be backfilled (Finam serves only the *live* book), so
history accrues from first run onward — never retroactively.

Rate-limit discipline is the same as everywhere: tickers are fetched **strictly
sequentially** with a throttle, never in parallel.

The spread is stored per-contract in **USD** (``spread_*_usd``); the RUB value the
chart shows is applied at query time via ``moex_fx_rates`` (single fx source).
The percentage metric is stored in **percent**; the frontend displays it in
basis points (×100).
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.timescale import (
    get_latest_usdrub,
    upsert_moex_spread_buckets,
    upsert_spb_spread_buckets,
)
from app.moex.config import MOEX_CRYPTO_FUTURES, MOEX_FORTS_MIC
from app.moex.fetcher import resolve_front_secids
from app.spb.config import SPB_LOTS, SPB_TICKERS
from app.spb.fetcher import FinamClient
from app.spb.orderbook import avg_spread_on_volume, parse_levels, spread_pct_on_volume

log = logging.getLogger(__name__)

# Per-side depth targets in roubles → column suffix (that much on the bid AND on
# the ask).
_TARGETS: dict[str, float] = {"1m": 1_000_000.0, "10m": 10_000_000.0}
# Metrics per sample: absolute per-contract USD spread AND the unit-free
# percentage spread (P_aver_ask-P_aver_bid)/top-of-book-mid*100, per target.
_METRICS: tuple[str, ...] = ("1m_usd", "10m_usd", "1m_pct", "10m_pct")
_BUCKET_SEC = 900         # one chart point per 15 min (:00/:15/:30/:45 labels)
_SAMPLE_SEC = 60          # one sweep per wall-clock minute (~15 samples/point)
                          # MM overrides this (its sweep is much longer) — see
                          # ``_next_minute(step)``.
_THROTTLE_SEC = 0.5       # between individual Finam calls (matches ETL)
_WAIT_SLICE_SEC = 30      # wall-clock is re-read this often while waiting for
                          # the next minute — a host suspend can't strand the loop

# Trading hours (Moscow time, UTC+3) — the spread is only sampled while MOEX/SPB
# are open: weekdays 07:00–23:45, weekends 10:00–19:00.  Off-hours minutes are
# skipped (no rows written), and the frontend cuts those gaps from the x-axis
# (``TRADE_WINDOW`` in ``OrderBookViz.tsx`` mirrors these bounds — keep in sync).
#
# The FORTS evening session actually runs to 23:50, but sampling stops at 23:45:
# a sample taken after it lands in the 23:45 bucket, whose label (the interval
# END) is 00:00 — outside the rendered window, so the point would be cut from the
# x-axis.  Closing at 23:45 keeps every written point visible and still adds the
# 23:15/23:30/23:45 points that the old 23:00 cut-off dropped.
_MSK = timezone(timedelta(hours=3))
_WEEKDAY_CLOSE_H = 23.75         # 23:45 MSK
_WEEKEND_CLOSE_H = 19.0


def _is_trading_now(now_utc: datetime | None = None) -> bool:
    now = (now_utc or datetime.now(timezone.utc)).astimezone(_MSK)
    h = now.hour + now.minute / 60.0
    if now.weekday() < 5:            # Mon–Fri
        return 7.0 <= h < _WEEKDAY_CLOSE_H
    return 10.0 <= h < _WEEKEND_CLOSE_H          # Sat–Sun

# MOEX front-month cache (assetcode → (SECID, lot)), refreshed once per UTC day
# since FORTS contracts roll monthly.  Lot is turnover-derived (see fetcher).
_moex_secids: dict[str, tuple[str, float | None]] = {}
_moex_secids_day = None


def _bucket_start(dt: datetime) -> datetime:
    """15-minute bucket label for instant ``dt`` (epoch-aligned :00/:15/:30/:45)."""
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % _BUCKET_SEC, tz=timezone.utc)


def _next_minute(now: datetime, step: int = _SAMPLE_SEC) -> datetime:
    """Next sampling instant strictly after ``now`` (UTC), on the ``step``-second
    grid.  The grid is epoch-aligned, so every instance sweeps at the same
    instants — which is what makes bucket averages comparable across hosts.  A
    collector whose sweep cannot finish within ``_SAMPLE_SEC`` must pass its own
    (larger) ``step`` rather than silently skipping grid points at random."""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step + step, tz=timezone.utc)


def _fresh_acc() -> dict:
    """Per-ticker accumulator: metric → [sum, count] over the current bucket."""
    return {k: [0.0, 0] for k in _METRICS}


def _add_sample(acc: dict, sp: dict) -> None:
    for k in _METRICS:
        if sp.get(k) is not None:
            acc[k][0] += sp[k]
            acc[k][1] += 1


def _mean(acc: dict, metric: str) -> float | None:
    s, n = acc[metric]
    return (s / n) if n else None


def _avg_samples(accs: dict[str, dict], metric: str) -> float:
    """Mean number of samples that went into one row of the finished bucket —
    the sanity check that the sweep still keeps up with the sampling grid."""
    counts = [a[metric][1] for a in accs.values()]
    return sum(counts) / len(counts) if counts else 0.0


async def _ensure_moex_secids(usdrub: float) -> None:
    """Refresh the MOEX front-month (SECID, lot) map once per UTC day (contracts
    roll monthly).  ISS is blocking, so resolve in a thread; a failure keeps the
    last known map (empty on first-ever failure → MOEX simply has no sample)."""
    global _moex_secids, _moex_secids_day
    today = datetime.now(timezone.utc).date()
    if _moex_secids and _moex_secids_day == today:
        return
    assetcodes = [ac for ac, _lot in MOEX_CRYPTO_FUTURES.values()]
    loop = asyncio.get_event_loop()
    try:
        resolved = await loop.run_in_executor(None, resolve_front_secids, assetcodes, usdrub)
    except Exception as exc:  # noqa: BLE001 — keep last map
        log.warning("MOEX front-month resolve failed: %s", exc)
        return
    got = {ac: (s, lot) for ac, (s, lot) in resolved.items() if s}
    if got:
        _moex_secids = got
        _moex_secids_day = today


async def _spb_sample(client: FinamClient, ticker: str, usdrub: float) -> dict:
    lot = SPB_LOTS.get(ticker, 1.0)
    try:
        ob = await client.fetch_orderbook(ticker, retries=1)
        bids, asks = parse_levels(ob)
        sp = {}
        for k, v in _TARGETS.items():
            sp[f"{k}_usd"] = avg_spread_on_volume(bids, asks, lot, usdrub, v)
            sp[f"{k}_pct"] = spread_pct_on_volume(bids, asks, lot, usdrub, v)
        return sp
    except Exception as exc:  # noqa: BLE001 — treat as a "no data" sample
        log.debug("SPB spread %s fetch failed: %s", ticker, exc)
        return {k: None for k in _METRICS}


async def _moex_sample(client: FinamClient, assetcode: str, fallback_lot: float,
                       usdrub: float, spb_ticker: str) -> dict:
    entry = _moex_secids.get(assetcode)
    secid = entry[0] if entry else None
    moex_lot = entry[1] if (entry and entry[1]) else fallback_lot
    if not secid:
        return {k: None for k in _METRICS}
    try:
        ob = await client.fetch_orderbook(secid, mic=MOEX_FORTS_MIC, retries=1)
        bids, asks = parse_levels(ob)
        return {
            "1m_usd":  avg_spread_on_volume(bids, asks, moex_lot, usdrub, 1_000_000.0),
            "10m_usd": None,
            "1m_pct":  spread_pct_on_volume(bids, asks, moex_lot, usdrub, 1_000_000.0),
            "10m_pct": None,
        }
    except Exception as exc:  # noqa: BLE001 — "no data" sample
        log.debug("MOEX spread %s (%s) fetch failed: %s", spb_ticker, secid, exc)
        return {k: None for k in _METRICS}


def _flush(bucket: datetime, spb_acc: dict, moex_acc: dict) -> tuple[list, list]:
    """Rows for the completed ``bucket``: plain per-metric average of its samples.

    The row is labeled with the interval **end** (bucket start + 15 min): the
    17:45 point is the average over 17:30–17:45, so a point appears on the chart
    at its own labeled time, not a quarter-hour later."""
    label = bucket + timedelta(seconds=_BUCKET_SEC)
    rows = [
        (label, t, *(_mean(spb_acc[t], k) for k in _METRICS))
        for t in SPB_TICKERS
    ]
    moex_rows = [
        (label, t, _mean(moex_acc[t], "1m_usd"), _mean(moex_acc[t], "1m_pct"))
        for t in MOEX_CRYPTO_FUTURES
    ]
    return rows, moex_rows


async def spb_spread_collector_loop() -> None:
    if not settings.finam_api_token:
        log.info("SPB spread collector disabled (no Finam token)")
        return
    log.info("SPB spread collector started (60s sweeps → 15-min averages)")
    cur_bucket: datetime | None = None
    sweep_sec = 0.0           # duration of the last sweep, logged per bucket so
                              # a sweep outgrowing the sampling grid is visible
    spb_acc = {t: _fresh_acc() for t in SPB_TICKERS}
    moex_acc = {t: _fresh_acc() for t in MOEX_CRYPTO_FUTURES}
    while True:
        minute = _next_minute(datetime.now(timezone.utc))
        while True:
            remaining = (minute - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, _WAIT_SLICE_SEC))
        if not _is_trading_now(minute):      # MOEX/SPB closed → don't sample
            continue                          # (pending bucket flushes on reopen)
        try:
            b = _bucket_start(minute)
            if cur_bucket is not None and b != cur_bucket:
                rows, moex_rows = _flush(cur_bucket, spb_acc, moex_acc)
                n_avg = _avg_samples(spb_acc, "1m_usd")
                spb_acc = {t: _fresh_acc() for t in SPB_TICKERS}
                moex_acc = {t: _fresh_acc() for t in MOEX_CRYPTO_FUTURES}
                await upsert_spb_spread_buckets(rows)
                await upsert_moex_spread_buckets(moex_rows)
                log.info("SPB spread: wrote 15-min point %s (%d SPB + %d MOEX rows, "
                         "%.1f samples/row avg, last sweep %.1fs)",
                         rows[0][0].strftime("%H:%M"), len(rows), len(moex_rows),
                         n_avg, sweep_sec)
            cur_bucket = b

            usdrub = await get_latest_usdrub()
            if not usdrub:
                log.warning("SPB spread: no USDRUB yet, retrying next minute")
                continue
            await _ensure_moex_secids(usdrub)
            t0 = time.monotonic()
            async with FinamClient(settings.finam_api_token) as client:
                for ticker in SPB_TICKERS:
                    _add_sample(spb_acc[ticker], await _spb_sample(client, ticker, usdrub))
                    await asyncio.sleep(_THROTTLE_SEC)
                # MOEX crypto-futures overlay — same client (RTSX MIC), only the
                # 1 млн ₽ target.  Front-month SECID per assetcode from ISS.
                for spb_ticker, (assetcode, fallback_lot) in MOEX_CRYPTO_FUTURES.items():
                    _add_sample(moex_acc[spb_ticker],
                                await _moex_sample(client, assetcode, fallback_lot,
                                                   usdrub, spb_ticker))
                    await asyncio.sleep(_THROTTLE_SEC)
            sweep_sec = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 — self-heal, keep the loop alive
            log.warning("SPB spread collector cycle failed: %s", exc)
