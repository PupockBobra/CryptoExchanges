"""
Funding Rate API

GET /api/funding/current    — latest rates per symbol × exchange (Redis → DB fallback)
GET /api/funding/spreads    — ranked cross-exchange spread opportunities
GET /api/funding/history    — historical settled rates for backtesting
GET /api/funding/symbols    — list of symbols with stored data
GET /api/funding/heatmap    — settled funding per day × symbol × exchange
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query

from app.redis_client import get_redis
from app.db.timescale import (
    fetch_latest_funding_rates,
    fetch_funding_rate_history_db,
    fetch_funding_daily,
    fetch_funding_symbols,
)
from app.api.cache import ttl_cache

log    = logging.getLogger(__name__)
router = APIRouter()

# Funding periods per year: interval_hours → count
_PERIODS_PER_YEAR = {1: 8_760, 4: 2_190, 8: 1_095}


def _annualized(rate: float, interval_hours: int) -> float:
    """Convert a single-period rate to annualized percentage."""
    periods = _PERIODS_PER_YEAR.get(interval_hours, round(8_760 / interval_hours))
    return round(rate * periods * 100, 4)


# ── /current ──────────────────────────────────────────────────────────────────

@router.get("/current")
@ttl_cache()
async def get_current_rates():
    """
    Latest funding rates per symbol × exchange.
    Returns Redis-cached live data where available; falls back to latest
    settled rate from the DB.
    """
    r = await get_redis()
    live: list[dict] = []

    # Scan Redis for all cached current-rate keys (cursor is int, 0 = done)
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match="funding:current:*", count=200)
        for key in keys:
            try:
                raw = await r.get(key)
                if raw:
                    data = json.loads(raw)
                    rate = data.get("rate")
                    if rate is not None:
                        ih = int(data.get("interval_hours") or 8)
                        data["annualized_pct"]           = _annualized(float(rate), ih)
                        pred = data.get("predicted_rate")
                        data["predicted_annualized_pct"] = (
                            _annualized(float(pred), ih) if pred is not None else None
                        )
                    live.append(data)
            except Exception:
                pass
        if cursor == 0:
            break

    if live:
        live.sort(key=lambda x: (x.get("symbol", ""), x.get("exchange", "")))
        return {"rates": live, "source": "live", "count": len(live)}

    # DB fallback — latest settled row per symbol × exchange
    rows = await fetch_latest_funding_rates()
    db_rates = []
    for row in rows:
        ih   = int(row["interval_hours"])
        rate = float(row["rate"])
        db_rates.append({
            "symbol":                   row["symbol"],
            "exchange":                 row["exchange"],
            "rate":                     rate,
            "predicted_rate":           None,
            "predicted_annualized_pct": None,
            "next_funding_time":        None,
            "settlement_time":          row["time"].isoformat(),
            "interval_hours":           ih,
            "annualized_pct":           _annualized(rate, ih),
            "updated_at":               row["time"].isoformat(),
        })
    db_rates.sort(key=lambda x: (x["symbol"], x["exchange"]))
    return {"rates": db_rates, "source": "db", "count": len(db_rates)}


# ── /spreads ──────────────────────────────────────────────────────────────────

@router.get("/spreads")
async def get_spreads():
    """
    Cross-exchange funding-rate spread opportunities, sorted by annualized yield.

    For each symbol with data on ≥2 exchanges, finds the pair (long_exchange,
    short_exchange) that maximises the spread:
      • Go LONG on the exchange with the lowest rate  (pay less / earn if negative)
      • Go SHORT on the exchange with the highest rate (earn the high funding)
      • Net income = spread × periods_per_year
    """
    result = await get_current_rates()
    by_symbol: dict[str, list[dict]] = {}
    for entry in result["rates"]:
        sym  = entry.get("symbol")
        rate = entry.get("rate")
        if sym and rate is not None:
            by_symbol.setdefault(sym, []).append(entry)

    spreads = []
    for symbol, entries in by_symbol.items():
        if len(entries) < 2:
            continue
        sorted_entries = sorted(entries, key=lambda x: float(x["rate"]))
        lowest  = sorted_entries[0]
        highest = sorted_entries[-1]
        spread  = float(highest["rate"]) - float(lowest["rate"])
        if spread <= 0:
            continue
        ih = int(highest.get("interval_hours") or 8)
        spreads.append({
            "symbol":            symbol,
            "long_exchange":     lowest["exchange"],
            "short_exchange":    highest["exchange"],
            "long_rate":         float(lowest["rate"]),
            "long_rate_ann":     _annualized(float(lowest["rate"]), ih),
            "short_rate":        float(highest["rate"]),
            "short_rate_ann":    _annualized(float(highest["rate"]), ih),
            "spread":            spread,
            "annualized_pct":    _annualized(spread, ih),
            "interval_hours":    ih,
            "next_funding_time": highest.get("next_funding_time"),
            "all_rates":         [
                {
                    "exchange":       e["exchange"],
                    "rate":           float(e["rate"]),
                    "annualized_pct": e.get("annualized_pct"),
                }
                for e in sorted_entries
            ],
        })

    spreads.sort(key=lambda x: x["annualized_pct"], reverse=True)
    return {"spreads": spreads, "count": len(spreads)}


# ── /history ──────────────────────────────────────────────────────────────────

@router.get("/history")
async def get_history(
    symbol:   str           = Query(..., description="Canonical symbol, e.g. BTC/USDT:USDT"),
    exchange: str | None    = Query(None, description="Filter by exchange"),
    days:     int           = Query(90, ge=1, le=730, description="Look-back window in days"),
):
    """
    Historical settled funding rates.  Intended for backtesting.
    Returns one record per settlement period (8h or 1h depending on exchange).
    """
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    rows  = await fetch_funding_rate_history_db(
        symbol, exchange=exchange, since=since, limit=10_000
    )
    rates = [
        {
            "time":           row["time"].isoformat(),
            "exchange":       row["exchange"],
            "rate":           row["rate"],
            "annualized_pct": _annualized(row["rate"], int(row["interval_hours"])),
        }
        for row in rows
    ]
    return {
        "symbol":   symbol,
        "exchange": exchange,
        "days":     days,
        "count":    len(rates),
        "rates":    rates,
    }


# ── /heatmap ──────────────────────────────────────────────────────────────────

@router.get("/heatmap")
@ttl_cache()
async def get_heatmap(days: int = Query(30, ge=1, le=365)):
    """
    Daily funding per symbol × exchange for the instrument heatmap.
    `pct_day` is what the day actually paid; `pct_year` annualises its mean rate.
    """
    rows = await fetch_funding_daily(days)
    return {
        "days": days,
        "rows": [
            {
                "date":        str(r["date"]),
                "symbol":      r["symbol"],
                "exchange":    r["exchange"],
                "pct_day":     round(float(r["pct_day"]), 6),
                "pct_year":    round(float(r["pct_year"]), 4) if r["pct_year"] is not None else None,
                "settlements": r["settlements"],
            }
            for r in rows
        ],
    }


# ── /symbols ──────────────────────────────────────────────────────────────────

@router.get("/symbols")
async def get_symbols():
    """List all symbols that have stored funding rate data."""
    symbols = await fetch_funding_symbols()
    return {"symbols": symbols}
