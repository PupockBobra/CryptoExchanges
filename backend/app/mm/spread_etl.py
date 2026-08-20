"""
MM FORTS "spread on volume" (AVG_SPREAD) collector — persistent 24/7 history.

Samples the live order book of **every** MM universe instrument once per
wall-clock 2-minute slot (during MOEX trading hours — see ``_SAMPLE_SEC``) and
writes the plain average as a 15-minute chart point: the absolute spread in the
instrument's quote unit and the unit-free percentage, for a 1 000 000 ₽-per-side
depth target.

Unlike the display feed (``mm.orderbook`` — lazy, only the open tab), this runs
for the whole universe regardless of which tab is open, so the history is
continuous.  Order-book depth can't be backfilled (Finam serves only the live
book), so history accrues from first run onward.

Wall-clock helpers, trading-hours gate and rate-limit discipline are shared with
the SPB collector so the two stay aligned: within a sweep it is one throttled
Finam call at a time, never a burst, ``retries=1``.  Note the two collectors do
run concurrently with each other (both are lifespan tasks on the same token), so
the process-wide REST rate is roughly two calls per throttle interval.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.timescale import upsert_mm_spread_buckets
from app.mm.config import MM_TARGET_RUB
from app.mm.universe import ensure_universe, get_universe
from app.spb.fetcher import FinamClient
from app.mm.config import MM_MIC
from app.spb.orderbook import avg_spread_on_volume, parse_levels, spread_pct_on_volume
from app.spb.spread_etl import (
    _BUCKET_SEC,
    _THROTTLE_SEC,
    _WAIT_SLICE_SEC,
    _avg_samples,
    _bucket_start,
    _is_trading_now,
    _next_minute,
)

log = logging.getLogger(__name__)

_METRICS = ("abs", "pct")

# Sampling grid, in seconds.  SPB sweeps 30 instruments and fits in a minute;
# MM sweeps ~100, which costs ~50 s of throttle alone plus one Finam round-trip
# each — it does NOT fit, so it samples every two minutes instead.  The point is
# not the rate but the *regularity*: on a 60 s grid the sweep finished in time
# only sometimes, so the gap between samples drifted between 60 s and 120 s
# depending on how fast Finam answered, and a plain average over irregular gaps
# is biased toward the periods when the venue responded quickly.  A grid the
# sweep always fits into restores both the even spacing and the cross-host
# alignment (both instances sample the same instants).
#
# 900 s / 120 s is not a whole number, so buckets alternate 8 and 7 samples.
# Check the "last sweep" figure in the per-bucket log line: if it settles well
# under 55 s, this can go back to 60.
_SAMPLE_SEC = 120


def _fresh_acc() -> dict:
    return {k: [0.0, 0] for k in _METRICS}


def _add_sample(acc: dict, sp: dict) -> None:
    for k in _METRICS:
        if sp.get(k) is not None:
            acc[k][0] += sp[k]
            acc[k][1] += 1


def _mean(acc: dict, metric: str) -> float | None:
    s, n = acc[metric]
    return (s / n) if n else None


async def _sample(client: FinamClient, inst: dict) -> dict:
    r = inst["step_ratio"]
    try:
        ob = await client.fetch_orderbook(inst["secid"], mic=MM_MIC, retries=1)
        bids, asks = parse_levels(ob)
        return {
            "abs": avg_spread_on_volume(bids, asks, r, 1.0, MM_TARGET_RUB),
            "pct": spread_pct_on_volume(bids, asks, r, 1.0, MM_TARGET_RUB),
        }
    except Exception as exc:  # noqa: BLE001 — treat as a "no data" sample
        log.debug("MM spread %s fetch failed: %s", inst["secid"], exc)
        return {k: None for k in _METRICS}


def _flush(bucket: datetime, acc: dict[str, dict], tickers: dict[str, dict]) -> list[tuple]:
    """Rows for the completed bucket, labeled with the interval END (bucket +
    15 min): plain per-metric average of the bucket's samples."""
    label = bucket + timedelta(seconds=_BUCKET_SEC)
    return [
        (label, t, tickers[t]["group"], _mean(acc[t], "abs"), _mean(acc[t], "pct"))
        for t in acc if t in tickers
    ]


async def mm_spread_collector_loop() -> None:
    if not settings.finam_api_token:
        log.info("MM spread collector disabled (no Finam token)")
        return
    log.info("MM spread collector started (%ds sweeps → 15-min averages)", _SAMPLE_SEC)
    cur_bucket: datetime | None = None
    sweep_sec = 0.0
    acc: dict[str, dict] = {}
    while True:
        minute = _next_minute(datetime.now(timezone.utc), _SAMPLE_SEC)
        while True:
            remaining = (minute - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, _WAIT_SLICE_SEC))
        if not _is_trading_now(minute):
            continue
        try:
            await ensure_universe()
            insts = get_universe()
            tickers = {i["ticker"]: i for i in insts}
            b = _bucket_start(minute)
            if cur_bucket is not None and b != cur_bucket:
                rows = _flush(cur_bucket, acc, tickers)
                n_avg = _avg_samples(acc, "abs")
                acc = {}
                n = await upsert_mm_spread_buckets(rows)
                log.info("MM spread: wrote 15-min point %s (%d rows, %.1f samples/row avg, "
                         "last sweep %.1fs)",
                         (cur_bucket + timedelta(seconds=_BUCKET_SEC)).strftime("%H:%M"),
                         n, n_avg, sweep_sec)
            cur_bucket = b

            for t, inst in tickers.items():
                acc.setdefault(t, _fresh_acc())
            t0 = time.monotonic()
            async with FinamClient(settings.finam_api_token) as client:
                for inst in insts:
                    _add_sample(acc[inst["ticker"]], await _sample(client, inst))
                    await asyncio.sleep(_THROTTLE_SEC)
            sweep_sec = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 — self-heal, keep the loop alive
            log.warning("MM spread collector cycle failed: %s", exc)
