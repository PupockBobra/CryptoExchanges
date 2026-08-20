"""
REST API for SPB Exchange perpetual-futures turnover (sourced from Finam TradeAPI).

Endpoints
---------
GET  /api/spb/daily-volume   → daily turnover (RUB) per ticker, last 30 days
GET  /api/spb/open-interest  → daily open interest per ticker, last 30 days
GET  /api/spb/funding        → all uploaded daily funding rows per ticker
POST /api/spb/funding/upload → ingest one or more funding CSVs (JSON body)
POST /api/spb/funding/exchange-refresh → read funding from СПБ Биржа's own feed
POST /api/spb/funding/tg-refresh → pull new funding CSVs from the Telegram channel
POST /api/spb/refresh        → trigger a Finam turnover ETL pass in the background
POST /api/spb/oi-refresh     → trigger an SPB-API open-interest ETL pass
"""

import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.config import settings
from app.db.timescale import (
    fetch_moex_spread_history,
    fetch_spb_daily_volume,
    fetch_spb_funding,
    fetch_spb_oi_daily,
    fetch_spb_spread_history,
    fetch_spb_weekly_adtv,
    get_latest_usdrub,
    upsert_spb_funding,
)
from app.spb.config import SPB_GROUPS, SPB_NAMES
from app.spb.funding import parse_funding_csv
from app.api.cache import clear_cache, ttl_cache

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/daily-volume")
@ttl_cache()
async def get_spb_daily_volume():
    """
    Daily SPB perp turnover in RUB per ticker for the last 30 days.
    Turnover is USD×USDRUBF; the current day is exact, history is approximated.
    """
    rows = await fetch_spb_daily_volume()
    return [
        {
            "date":         str(r["date"]),
            "date_label":   r["date_label"].strip(),
            "ticker":       r["ticker"],
            "name":         SPB_NAMES.get(r["ticker"], r["ticker"]),
            "group":        SPB_GROUPS.get(r["ticker"], "US Market"),
            "turnover_rub": float(r["turnover_rub"] or 0),
        }
        for r in rows
    ]


@router.get("/weekly-adtv")
@ttl_cache()
async def get_spb_weekly_adtv():
    """
    Weekly ADTV in RUB per SPB ticker × ISO week.
    Same RUB conversion as /daily-volume; the frontend pivots into per-ticker charts.
    """
    rows = await fetch_spb_weekly_adtv()
    return [
        {
            "week_start":   str(r["week_start"]),
            "week_label":   r["week_label"].strip(),
            "ticker":       r["ticker"],
            "name":         SPB_NAMES.get(r["ticker"], r["ticker"]),
            "group":        SPB_GROUPS.get(r["ticker"], "US Market"),
            "days_in_week": r["days_in_week"],
            "adtv":         float(r["adtv"] or 0),
        }
        for r in rows
    ]


@router.get("/open-interest")
@ttl_cache()
async def get_spb_open_interest():
    """
    Daily SPB perp open interest per ticker for the last 30 days.
    oi_rub is the open-position notional (USD×USDRUBF); oi_contracts is raw contracts.
    """
    rows = await fetch_spb_oi_daily()
    return [
        {
            "date":         str(r["date"]),
            "date_label":   r["date_label"].strip(),
            "ticker":       r["ticker"],
            "name":         SPB_NAMES.get(r["ticker"], r["ticker"]),
            "group":        SPB_GROUPS.get(r["ticker"], "US Market"),
            "oi_contracts": float(r["oi_contracts"] or 0),
            "oi_rub":       float(r["oi_rub"] or 0),
        }
        for r in rows
    ]


@router.get("/funding")
@ttl_cache()
async def get_spb_funding():
    """
    Every uploaded funding row (all history) per ticker.  Raw values from the
    channel's CSVs: pct_year / pct_day (funding rate %), fund_curr (per-contract
    funding, USD), mean_price / mean_index (day-average perp & index prices).
    The frontend pivots this into the by-day heatmap table.
    """
    rows = await fetch_spb_funding()
    return [
        {
            "date":       str(r["date"]),
            "ticker":     r["ticker"],
            "name":       SPB_NAMES.get(r["ticker"], r["ticker"]),
            "group":      SPB_GROUPS.get(r["ticker"], "US Market"),
            "pct_year":   None if r["pct_year"]   is None else float(r["pct_year"]),
            "pct_day":    None if r["pct_day"]    is None else float(r["pct_day"]),
            "fund_curr":  None if r["fund_curr"]  is None else float(r["fund_curr"]),
            "mean_price": None if r["mean_price"] is None else float(r["mean_price"]),
            "mean_index": None if r["mean_index"] is None else float(r["mean_index"]),
        }
        for r in rows
    ]


class FundingFile(BaseModel):
    name: str
    text: str


class FundingUpload(BaseModel):
    files: list[FundingFile]


@router.post("/funding/upload")
async def upload_spb_funding(payload: FundingUpload):
    """
    Ingest one or more funding CSVs sent as JSON ``{files: [{name, text}]}``.
    Each file is parsed (date from filename, tickers from the ``Neo`` column) and
    upserted, so re-uploading a day overwrites it.  Returns a per-file report so
    the page can show what was accepted or skipped.
    """
    all_rows: list[tuple] = []
    results = []
    for f in payload.files:
        rows, error = parse_funding_csv(f.name, f.text)
        if error:
            results.append({"name": f.name, "ok": False, "rows": 0, "error": error})
            continue
        all_rows.extend(rows)
        results.append({"name": f.name, "ok": True, "rows": len(rows), "date": str(rows[0][0])})

    saved = await upsert_spb_funding(all_rows)
    clear_cache()          # the funding heatmap is TTL-cached — show the upload now
    accepted = sum(1 for r in results if r["ok"])
    return {"saved": saved, "files": len(payload.files), "accepted": accepted, "results": results}


@router.post("/funding/exchange-refresh")
async def refresh_spb_funding_from_exchange():
    """
    Read funding straight from СПБ Биржа's public feed now, instead of waiting
    for the next pass inside the publication window.

    Runs inline (one HTTP call, 25 records) and returns what was stored.  Outside
    the window (23:00 → 11:30 МСК) the feed reports no settled figures, so the
    honest answer is ``rows: 0`` rather than a board of fake zeros.
    """
    from app.spb.funding_exchange import ingest_funding_from_exchange

    return await ingest_funding_from_exchange()


@router.post("/funding/tg-refresh")
async def refresh_spb_funding_from_telegram(background: BackgroundTasks):
    """
    Pull any new funding CSVs from the @beststocks_neo channel now, instead of
    waiting for the hourly loop.  Runs in the background (a first, historical
    scan downloads dozens of files and would outlive nginx's 60 s proxy timeout)
    — the result lands in the logs and, for a normal window, in the heatmap
    seconds later.
    """
    from app.spb.funding_tg import ingest_funding_from_telegram

    async def _run() -> None:
        try:
            res = await ingest_funding_from_telegram()
            log.info("SPB funding TG refresh: %s", res)
        except Exception as e:                          # noqa: BLE001
            log.warning("SPB funding TG refresh failed: %r", e)

    background.add_task(_run)
    return {"status": "started"}


@router.get("/orderbook")
async def get_spb_orderbook():
    """
    Live order book for every SPB perp, served from the in-memory cache kept
    warm by the background poller (``app.spb.orderbook``) — not a per-request
    Finam call.  Reading renews the poller's active window, so it keeps
    refreshing while the page is open and idles otherwise.

    Prices are in USD (the instrument's quote currency), sizes in contracts.
    The cache is pre-seeded with placeholder cards, so the first load renders all
    instruments immediately and the poller fills their levels progressively.
    """
    from app.spb.orderbook import get_cached_orderbooks

    if not settings.finam_api_token:
        return []

    return get_cached_orderbooks()


@router.get("/moex-orderbook")
async def get_moex_orderbook():
    """
    Live order book for the 5 MOEX crypto-index futures (BTC/ETH/SOL/XRP/TRX),
    served from the same warm cache and streamed via Finam under MIC RTSX.  Each
    book is keyed by the SPB crypto ticker it maps to, so the frontend shows it to
    the right of that crypto card.  Prices in USD, sizes in contracts.  Reading
    renews the poller's active window (same feed as /orderbook).
    """
    from app.spb.orderbook import get_cached_moex_orderbooks

    if not settings.finam_api_token:
        return []

    return get_cached_moex_orderbooks()


@router.get("/spread-history")
@ttl_cache()
async def get_spb_spread_history(days: int = 7):
    """
    Time-weighted "spread on volume" (AVG_SPREAD) history per SPB perp, in 15-min
    buckets.  Depth target 1 млн ₽ *per side*, in two units: absolute
    ``spread_usd`` (raw USD price gap P_aver_ask−P_aver_bid, no fx, no lot) and
    unit-free ``spread_pct`` (absolute / top-of-book mid × 100).  Values are
    null when a side lacked the target depth (illiquid → the chart bridges the
    gap).  History accrues from first run; it cannot be backfilled.

    For the 5 crypto tickers, an additional ``moex`` object carries the MOEX
    crypto-index futures spread (1 млн ₽ per side, same methodology) so the
    frontend can overlay a MOEX line on the SPB line.

    **Columnar payload**: each series is parallel arrays (``buckets`` /
    ``spread_usd`` / ``spread_pct``), not a list of point objects.  The chart
    feeds Plotly arrays anyway, and 25 tickers × 7 days × 15-min points repeated
    the four field names ~17 000 times — three quarters of a 1.3 MB response was
    punctuation.
    """
    def series(ticker: str) -> dict:
        entry = by_ticker.get(ticker)
        if entry is None:
            entry = by_ticker[ticker] = {
                "ticker":     ticker,
                "name":       SPB_NAMES.get(ticker, ticker),
                "group":      SPB_GROUPS.get(ticker, "US Market"),
                "buckets":    [],
                "spread_usd": [],
                "spread_pct": [],
                "moex":       None,
            }
        return entry

    by_ticker: dict[str, dict] = {}
    for r in await fetch_spb_spread_history(days):
        entry = series(r["ticker"])
        entry["buckets"].append(r["bucket"].isoformat())
        entry["spread_usd"].append(None if r["spread_1m_usd"] is None else float(r["spread_1m_usd"]))
        entry["spread_pct"].append(None if r["spread_1m_pct"] is None else float(r["spread_1m_pct"]))

    # MOEX crypto-futures overlay — attach to the matching SPB crypto card.  Its
    # buckets are collected in the same sweep but can start/stop independently,
    # so it carries its own x-axis rather than sharing the SPB one.
    for r in await fetch_moex_spread_history(days):
        entry = series(r["ticker"])
        moex = entry["moex"]
        if moex is None:
            moex = entry["moex"] = {"buckets": [], "spread_usd": [], "spread_pct": []}
        moex["buckets"].append(r["bucket"].isoformat())
        moex["spread_usd"].append(None if r["spread_1m_usd"] is None else float(r["spread_1m_usd"]))
        moex["spread_pct"].append(None if r["spread_1m_pct"] is None else float(r["spread_1m_pct"]))
    return list(by_ticker.values())


@router.get("/spread-live")
async def get_spb_spread_live():
    """
    Instantaneous spread-on-volume (AVG_SPREAD, $) per ticker, computed from the
    live in-memory order-book cache — tick-by-tick fresh while the page is open.
    The frontend polls this every couple of seconds to move the chart's tail
    point in real time; completed 15-min buckets come from /spread-history.
    Reading renews the book feed's active window.  ``usdrub`` still sizes the
    1 млн ₽ fill depth, but the returned spread is the raw USD price gap.
    """
    from datetime import datetime, timezone

    from app.spb.orderbook import compute_live_spreads, note_access

    if not settings.finam_api_token:
        return []
    note_access()
    usdrub = await get_latest_usdrub()
    if not usdrub:
        return []
    ts = datetime.now(timezone.utc).isoformat()
    return [
        {
            "ticker":         r["ticker"],
            "ts":             ts,
            "spread_1m_usd":  r["spread_1m_usd"],
            "spread_10m_usd": r["spread_10m_usd"],
        }
        for r in compute_live_spreads(usdrub)
    ]


@router.post("/refresh")
async def trigger_spb_refresh(background_tasks: BackgroundTasks):
    """Kick off a Finam SPB turnover ETL pass in the background."""
    from app.spb.etl import run_spb_etl
    background_tasks.add_task(run_spb_etl)
    return {"status": "spb etl started"}


@router.post("/oi-refresh")
async def trigger_spb_oi_refresh(background_tasks: BackgroundTasks):
    """Kick off an SPB-API open-interest ETL pass in the background."""
    from app.spb.oi_etl import run_spb_oi_etl
    background_tasks.add_task(run_spb_oi_etl)
    return {"status": "spb oi etl started"}
