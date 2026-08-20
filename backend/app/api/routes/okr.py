"""
OKR — MOEX mirror contracts as a share of the crypto exchanges' TradFi turnover.

GET  /api/okr/ratio?days=30
        → { days, points: [{ date, date_label, moex_rub, crypto_rub, ratio_pct }],
            latest, avg_pct, baskets }
          Only complete MOEX trading days; see fetch_okr_ratio for why.
POST /api/okr/refresh   → run the FORTS day sweep in the background.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Query

from app.api.cache import ttl_cache
from app.db.timescale import fetch_okr_ratio
from app.okr.config import (
    COMMODITY_BASKET,
    FOREIGN_SECURITIES_BASKET,
    NUMERATOR_ASSETS,
    TRADFI_OHLCV_BASES,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/ratio")
@ttl_cache()
async def get_okr_ratio(days: int = Query(30, ge=1, le=90)):
    records = await fetch_okr_ratio(days, list(NUMERATOR_ASSETS), list(TRADFI_OHLCV_BASES))

    points = []
    for r in records:
        moex   = float(r["moex_rub"] or 0)
        crypto = float(r["crypto_rub"] or 0)
        points.append({
            "date":       str(r["date"]),
            "date_label": r["date_label"].strip(),
            "moex_rub":   moex,
            "crypto_rub": crypto,
            "ratio_pct":  round(moex / crypto * 100, 4) if crypto else None,
        })

    # Baseline the KPI compares against: the mean of the daily ratios actually
    # drawn on the chart (not ΣMOEX/Σcrypto, which would weight the big days).
    ratios  = [p["ratio_pct"] for p in points if p["ratio_pct"] is not None]
    avg_pct = round(sum(ratios) / len(ratios), 4) if ratios else None

    return {
        "days":    days,
        "points":  points,
        "latest":  points[-1] if points else None,
        "avg_pct": avg_pct,
        "baskets": {
            "commodity": list(COMMODITY_BASKET),
            "foreign":   list(FOREIGN_SECURITIES_BASKET),
        },
    }


@router.post("/refresh")
async def trigger_okr_refresh(background_tasks: BackgroundTasks):
    """Kick off a FORTS day sweep in the background."""
    from app.okr.etl import run_okr_etl_safe
    background_tasks.add_task(run_okr_etl_safe)
    return {"status": "okr etl started"}
