"""
REST API for MM (market-maker) FORTS futures — live order book + spread-on-volume
per FORTS collection.  Data: contract classification + codes from ISS, live books
from Finam (MIC RTSX).  Same shape as the SPB Order Book endpoints.

Endpoints
---------
GET  /api/mm/groups                       → the six FORTS collections + counts
GET  /api/mm/orderbook?group=<id>         → live books for one group (warm cache)
GET  /api/mm/spread-history?group=&days=  → 15-min spread history for one group
GET  /api/mm/spread-live?group=<id>       → instantaneous spread from the cache
POST /api/mm/refresh                       → rebuild the ISS universe now
"""

import logging

from fastapi import APIRouter

from app.config import settings
from app.db.timescale import fetch_mm_spread_history
from app.mm.config import MM_GROUPS, MM_LABEL
from app.mm.universe import ensure_universe, get_group, get_universe
from app.api.cache import ttl_cache

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/groups")
async def get_mm_groups():
    """The six FORTS collections (id + label) with the count of liquid
    front-month instruments currently in each, so the UI can label tabs."""
    await ensure_universe()
    counts: dict[str, int] = {}
    for i in get_universe():
        counts[i["group"]] = counts.get(i["group"], 0) + 1
    return [
        {"id": g["id"], "label": g["label"], "count": counts.get(g["id"], 0)}
        for g in MM_GROUPS
    ]


@router.get("/orderbook")
async def get_mm_orderbook(group: str):
    """Live order books for one group's front-month instruments, from the warm
    in-memory cache (streamed via Finam under MIC RTSX).  Reading renews this
    group's feed window so it keeps refreshing while the tab is open.  Prices in
    the instrument's quote currency, sizes in contracts."""
    from app.mm.orderbook import get_cached_orderbooks

    if not settings.finam_api_token:
        return []
    await ensure_universe()
    return get_cached_orderbooks(group)


@router.get("/spread-history")
@ttl_cache()
async def get_mm_spread_history(group: str, days: int = 7):
    """15-min "spread on volume" history for one group.  Depth target 1 млн ₽
    per side; ``spread_abs`` in the instrument's quote unit (₽ / $ / points),
    ``spread_pct`` unit-free.  ``currency`` labels the absolute axis.  Nulls =
    illiquid (chart bridges the gap).  History accrues from first run.

    Columnar payload (parallel ``buckets`` / ``spread_abs`` / ``spread_pct``
    arrays) — same reasoning as the SPB endpoint: the shares tab alone carried
    ~65 instruments × 7 days of 15-min points, and per-point field names were
    most of the ~1.9 MB."""
    await ensure_universe()
    meta = {i["ticker"]: i for i in get_group(group)}
    by_ticker: dict[str, dict] = {}
    for r in await fetch_mm_spread_history(group, days):
        t = r["ticker"]
        entry = by_ticker.get(t)
        if entry is None:
            inst = meta.get(t, {})
            entry = by_ticker[t] = {
                "ticker":     t,
                "name":       inst.get("name", t),
                "group":      group,
                "currency":   inst.get("currency", "₽"),
                "buckets":    [],
                "spread_abs": [],
                "spread_pct": [],
            }
        entry["buckets"].append(r["bucket"].isoformat())
        entry["spread_abs"].append(None if r["spread_abs"] is None else float(r["spread_abs"]))
        entry["spread_pct"].append(None if r["spread_pct"] is None else float(r["spread_pct"]))
    # Keep universe order (liquid tickers with no history yet still get a card
    # via /orderbook; here we only return tickers that have points).
    return list(by_ticker.values())


@router.get("/spread-live")
async def get_mm_spread_live(group: str):
    """Instantaneous spread-on-volume per ticker in a group, from the live cache
    (tick-by-tick fresh while the tab is open).  ``spread_abs`` in quote units.
    Reading renews the feed window."""
    from datetime import datetime, timezone

    from app.mm.orderbook import compute_live_spreads, note_access

    if not settings.finam_api_token:
        return []
    note_access(group)
    ts = datetime.now(timezone.utc).isoformat()
    return [
        {"ticker": r["ticker"], "ts": ts,
         "spread_abs": r["spread_abs"], "spread_pct": r["spread_pct"]}
        for r in compute_live_spreads(group)
    ]


@router.post("/refresh")
async def trigger_mm_refresh():
    """Force an immediate ISS universe rebuild (front-month roll, new listings)."""
    from app.mm import universe as u

    u._universe_day = None      # invalidate the daily cache
    built = await ensure_universe()
    return {"status": "mm universe rebuilt", "instruments": len(built)}
