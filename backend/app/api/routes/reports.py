"""
REST API for the Custom Report builder.

Endpoints
---------
GET /api/reports/options?metric=volume|open_interest|price|funding
    → { metric, instruments[], exchanges[], date_min, date_max }
GET /api/reports/tree?metric=…
    → hierarchical picker: [{id,label,children:[…]}] with leaf instruments
      {symbol, exchange, label}.  Cryptoexchanges → exchange → asset class;
      SPB futures / MOEX forts → asset class.
GET /api/reports/data
    ?metric=volume
    &pairs=binance~BTC/USDT,spb~AMZNperpA   (preferred: exact exchange×symbol)
    &symbols=BTC/USDT,ETH/USDT              (legacy: symbols × exchanges cross)
    &exchanges=binance,okx                  (optional; omit for all)
    &from=2026-01-01&to=2026-07-01          (required, YYYY-MM-DD)
    &agg=daily|weekly|monthly               (default daily)
    &currency=rub|usd                       (default rub; money metrics only)
    → flat list of { bucket, bucket_label, symbol, exchange, value }

The instrument universe differs per metric (funding/price are crypto-only,
open-interest is crypto+SPB, volume spans all four sources), so the frontend
should refetch /tree whenever the metric changes.
"""

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.db.timescale import (
    REPORT_METRICS, fetch_report, fetch_report_options, fetch_report_tree,
)

router = APIRouter()


def _split(csv: str | None) -> list[str] | None:
    if not csv:
        return None
    items = [x.strip() for x in csv.split(",") if x.strip()]
    return items or None


@router.get("/options")
async def get_report_options(
    metric: str = Query(..., description="volume | open_interest | price | funding"),
):
    if metric not in REPORT_METRICS:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric}")
    return await fetch_report_options(metric)


@router.get("/tree")
async def get_report_tree(
    metric: str = Query(..., description="volume | open_interest | price | funding"),
):
    if metric not in REPORT_METRICS:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric}")
    return await fetch_report_tree(metric)


@router.get("/data")
async def get_report_data(
    metric:    str        = Query(..., description="volume | open_interest | price | funding"),
    from_:     date       = Query(..., alias="from"),
    to:        date       = Query(...),
    pairs:     str | None = Query(None, description="Comma-separated exchange~symbol pairs"),
    symbols:   str | None = Query(None, description="Comma-separated symbols (legacy)"),
    exchanges: str | None = Query(None, description="Comma-separated; omit for all"),
    agg:       str        = Query("daily", pattern="^(daily|weekly|monthly)$"),
    currency:  str        = Query("rub", pattern="^(rub|usd)$"),
):
    if metric not in REPORT_METRICS:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric}")
    if from_ > to:
        raise HTTPException(status_code=400, detail="from must be <= to")

    # Preferred path: exact (exchange, symbol) pairs from the tree picker.
    pair_set: set[tuple[str, str]] | None = None
    if pairs:
        parsed = [p.split("~", 1) for p in pairs.split(",") if "~" in p]
        pair_set = {(e, s) for e, s in parsed}
        syms = sorted({s for _, s in parsed})
        exs  = sorted({e for e, _ in parsed})
    else:
        syms = _split(symbols)
        exs  = _split(exchanges)
    if not syms:
        raise HTTPException(status_code=400, detail="pairs or symbols is required")

    rows = await fetch_report(
        metric=metric,
        symbols=syms,
        exchanges=exs,
        date_from=from_,
        date_to=to,
        agg=agg,
        currency=currency,
    )
    if pair_set is not None:
        rows = [r for r in rows if (r["exchange"], r["symbol"]) in pair_set]

    return [
        {
            "bucket":       str(r["bucket"]),
            "bucket_label": r["bucket_label"].strip(),
            "symbol":       r["symbol"],
            "exchange":     r["exchange"],
            "value":        float(r["value"] or 0),
        }
        for r in rows
    ]
