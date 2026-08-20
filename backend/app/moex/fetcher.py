"""
MOEX ISS history fetcher.

Uses curl_cffi (Chrome TLS fingerprint) to bypass ISS's JA3-based filtering.
Set MOEX_ISS_PROXY env var (e.g. socks5://user:pass@host:port) for environments
where the server IP is geo-blocked by MOEX.

All public functions return plain Python dicts — no async, so they can be called
from a ThreadPoolExecutor inside the async ETL loop without blocking the event loop.
"""

import logging
import math
import os
import time
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

_ISS_BASE = "https://iss.moex.com/iss"
_PROXY    = os.environ.get("MOEX_ISS_PROXY")          # optional socks5/http proxy
_PAGESIZE = 100                                         # ISS always returns ≤100 rows


def _make_session():
    """Create a curl_cffi Session that looks like Chrome."""
    try:
        from curl_cffi.requests import Session
        kwargs: dict[str, Any] = {"impersonate": "chrome124"}
        if _PROXY:
            kwargs["proxies"] = {"https": _PROXY, "http": _PROXY}
        return Session(**kwargs)
    except ImportError:
        # Fallback: plain requests (works when ISS is reachable without TLS tricks)
        import requests
        s = requests.Session()
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; ArbiTracker/1.0)"
        )
        if _PROXY:
            s.proxies = {"https": _PROXY, "http": _PROXY}
        return s


def _get(session, url: str, params: dict, retries: int = 3) -> dict:
    """HTTP GET with retry/backoff.  Returns parsed JSON."""
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last_exc = exc
            log.warning("ISS request failed (attempt %d/%d): %s", attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"ISS unreachable after {retries} attempts: {last_exc}")


def fetch_secid_history(
    secid: str,
    from_date: date,
    till_date: date,
    columns: str = "SECID,TRADEDATE,VALUE,WAPRICE,CLOSE",
) -> list[dict]:
    """
    Fetch all history rows for a single SECID between from_date and till_date.
    Handles ISS pagination transparently (100 rows/page).
    Returns a list of dicts keyed by the requested columns.
    """
    url = f"{_ISS_BASE}/history/engines/futures/markets/forts/securities/{secid}.json"
    params = {
        "from":             from_date.isoformat(),
        "till":             till_date.isoformat(),
        "history.columns":  columns,
        "start":            0,
    }

    rows: list[dict] = []
    with _make_session() as session:
        while True:
            data = _get(session, url, params)
            hist     = data["history"]
            cols     = hist["columns"]
            page     = hist["data"]
            cursor   = data["history.cursor"]["data"][0]
            idx, total, pagesize = cursor[0], cursor[1], cursor[2]

            for row in page:
                rows.append(dict(zip(cols, row)))

            log.debug("ISS %s: fetched %d/%d rows (start=%d)", secid, len(rows), total, idx)

            next_start = idx + pagesize
            if next_start >= total:
                break
            params = dict(params, start=next_start)

    return rows


def fetch_usdrubf_history(from_date: date, till_date: date) -> list[dict]:
    """
    Fetch daily WAPRICE / CLOSE for the perpetual USD/RUB futures (USDRUBF).
    Returns list of {"TRADEDATE": "YYYY-MM-DD", "WAPRICE": float, "CLOSE": float}.
    """
    from app.moex.config import USDRUBF_SECID
    return fetch_secid_history(
        USDRUBF_SECID,
        from_date,
        till_date,
        columns="TRADEDATE,WAPRICE,CLOSE",
    )


def _discover_secids_for_assetcode(
    session,
    asset_code: str,
    from_date: date,
    till_date: date,
) -> list[str]:
    """
    Discover all SECID codes (including expired contracts) for a given ASSETCODE
    by querying the ISS market-level endpoint once per month boundary.

    The market-level endpoint only returns one session at a time (the `date` param),
    so we sample the first day of each month in [from_date, till_date].  This is
    cheap (≤7 requests for a 6-month window) and catches every contract that was
    front-month at any point in the period.

    Spreads are excluded: their SECIDs are formed by concatenating two contract
    codes (e.g. BRG6BRH6) and are therefore longer than a single contract code.
    We keep only SECIDs whose length is at most len(asset_code) + 2 (code + month
    letter + year digit).
    """
    url = f"{_ISS_BASE}/history/engines/futures/markets/forts/securities.json"
    max_len = len(asset_code) + 2   # e.g. "BR" + "G" + "6" = 4 chars

    secids: set[str] = set()

    # Build list of sample dates: first of each month + till_date
    sample_dates: list[date] = []
    d = from_date.replace(day=1)
    while d <= till_date:
        sample_dates.append(d)
        month = d.month + 1 if d.month < 12 else 1
        year  = d.year if d.month < 12 else d.year + 1
        d = d.replace(year=year, month=month, day=1)
    sample_dates.append(till_date)

    # Also try the last few days before till_date to guarantee at least one
    # recent trading day is hit regardless of holidays or same-day data lag.
    for offset in range(1, 6):
        extra = till_date - timedelta(days=offset)
        if extra >= from_date and extra not in sample_dates:
            sample_dates.append(extra)

    for sample_date in sample_dates:
        try:
            data = _get(session, url, {
                "assetcode":        asset_code,
                "date":             sample_date.isoformat(),
                "history.columns":  "SECID",
                "start":            0,
            })
            for row in data["history"]["data"]:
                secid = row[0]
                if secid and len(secid) <= max_len:
                    secids.add(secid)
        except Exception as exc:
            log.warning("ISS SECID discovery failed for %s on %s: %s", asset_code, sample_date, exc)

    log.info("ISS discovered %d SECIDs for assetcode=%s: %s", len(secids), asset_code, sorted(secids))
    return list(secids)


def _snap_lot(x: float) -> float | None:
    """Snap a raw turnover-derived lot to 1 significant figure.  Contract lots are
    clean round numbers (0.001 / 0.01 / 1 / 100 …); the raw ratio comes out as
    e.g. 0.984 or 98.9 due to intraday fx/price drift, so 1 sig-fig recovers the
    canonical value."""
    if x <= 0:
        return None
    return round(x, -math.floor(math.log10(x)))


def resolve_front_secids(
    assetcodes: list[str],
    usdrub: float,
    on_date: date | None = None,
) -> dict[str, tuple[str | None, float | None]]:
    """
    Resolve, per ASSETCODE, the front-month SECID and its lot (coins/contract) —
    on the most recent trading day.

    FORTS contracts roll monthly, so the live feed can't hard-code a SECID.  We
    sample the ISS market-level history endpoint for each assetcode, walking back
    up to a week to skip weekends/holidays, and pick the single contract with the
    highest VOLUME (the front month).  Calendar-spread SECIDs (concatenations like
    ``BTN6BTQ6``) are excluded by length.

    The lot is derived from that day's turnover the same way it was verified
    against Finam — ``VALUE / (VOLUME × SETTLEPRICE × usdrub)`` (VALUE is roubles,
    price is USD) — then snapped to 1 significant figure.  ``None`` lot when the
    day had no volume (caller falls back to the config lot).

    Returns ``{assetcode: (SECID|None, lot|None)}``.  Synchronous (ISS is
    blocking) — call from a thread executor inside the async collector.
    """
    url = f"{_ISS_BASE}/history/engines/futures/markets/forts/securities.json"
    today = on_date or date.today()
    out: dict[str, tuple[str | None, float | None]] = {}
    with _make_session() as session:
        for ac in assetcodes:
            max_len = len(ac) + 2          # single contract; spreads are longer
            picked: dict | None = None
            for offset in range(0, 7):     # walk back to the last trading day
                d = today - timedelta(days=offset)
                try:
                    data = _get(session, url, {
                        "assetcode":       ac,
                        "date":            d.isoformat(),
                        "history.columns": "SECID,VOLUME,VALUE,SETTLEPRICE",
                        "start":           0,
                    })
                except Exception as exc:
                    log.warning("ISS front-month lookup failed for %s on %s: %s", ac, d, exc)
                    continue
                cols = data["history"]["columns"]
                best, best_vol = None, -1.0
                for r in data["history"]["data"]:
                    row = dict(zip(cols, r))
                    secid = row.get("SECID")
                    vol = float(row.get("VOLUME") or 0)
                    if secid and len(secid) <= max_len and vol > best_vol:
                        best, best_vol = row, vol
                if best:
                    picked = best
                    break
            if not picked:
                out[ac] = (None, None)
                continue
            secid = picked["SECID"]
            vol    = float(picked.get("VOLUME") or 0)
            value  = float(picked.get("VALUE") or 0)
            settle = float(picked.get("SETTLEPRICE") or 0)
            lot = None
            if vol > 0 and settle > 0 and usdrub and usdrub > 0:
                lot = _snap_lot(value / (vol * settle * usdrub))
            out[ac] = (secid, lot)
            log.info("MOEX front-month %s: secid=%s lot=%s", ac, secid, lot)
    return out


def aggregate_asset_value_by_assetcode(
    asset_code: str,
    from_date: date,
    till_date: date,
) -> dict[date, float]:
    """
    Fetch VALUE for ALL instruments with the given ASSETCODE (including expired
    contracts) over [from_date, till_date].

    Two-step process:
      1. Discover all SECID codes that traded under this assetcode during the period
         (samples the ISS market-level endpoint at monthly boundaries).
      2. Fetch full history per SECID and sum VALUE by date — same as
         aggregate_asset_value() but with a dynamically built SECID list.

    This replaces the old hardcoded SERIES_BY_ASSET approach, which missed expired
    contracts and therefore under-counted historical ADTV.
    """
    with _make_session() as session:
        secids = _discover_secids_for_assetcode(session, asset_code, from_date, till_date)
        if not secids:
            log.warning("ISS: no SECIDs discovered for assetcode=%s", asset_code)
            return {}

        totals: dict[date, float] = {}
        for secid in secids:
            url = (
                f"{_ISS_BASE}/history/engines/futures/markets/forts"
                f"/securities/{secid}.json"
            )
            params = {
                "from":             from_date.isoformat(),
                "till":             till_date.isoformat(),
                "history.columns":  "TRADEDATE,VALUE",
                "start":            0,
            }
            while True:
                data   = _get(session, url, params)
                hist   = data["history"]
                cols   = hist["columns"]
                page   = hist["data"]
                cursor = data["history.cursor"]["data"][0]
                idx, total, pagesize = cursor[0], cursor[1], cursor[2]

                for row in page:
                    r   = dict(zip(cols, row))
                    val = r.get("VALUE")
                    if not val:
                        continue
                    trade_date = date.fromisoformat(r["TRADEDATE"])
                    totals[trade_date] = totals.get(trade_date, 0.0) + float(val)

                next_start = idx + pagesize
                if next_start >= total:
                    break
                params = dict(params, start=next_start)

        log.info(
            "ISS assetcode=%s: aggregated %d trading days from %d contracts",
            asset_code, len(totals), len(secids),
        )
        return totals


def aggregate_asset_oi_by_assetcode(
    asset_code: str,
    from_date: date,
    till_date: date,
) -> dict[date, tuple[float, float]]:
    """
    Open interest for ALL series of one ASSETCODE over [from_date, till_date],
    summed per date → {date: (contracts, value_rub)}.

    Same two-step discovery as ``aggregate_asset_value_by_assetcode`` (the OI of
    an asset is spread across its live series, so a single front-month SECID
    would undercount).  ISS serves both figures per series and date:
    ``OPENPOSITION`` (contracts) and ``OPENPOSITIONVALUE`` (roubles) — no lot or
    step-price math needed on our side.  Both are one-sided, as MOEX publishes
    them.
    """
    with _make_session() as session:
        secids = _discover_secids_for_assetcode(session, asset_code, from_date, till_date)
        if not secids:
            log.warning("ISS: no SECIDs discovered for assetcode=%s (OI)", asset_code)
            return {}

        totals: dict[date, tuple[float, float]] = {}
        for secid in secids:
            url = (
                f"{_ISS_BASE}/history/engines/futures/markets/forts"
                f"/securities/{secid}.json"
            )
            params = {
                "from":            from_date.isoformat(),
                "till":            till_date.isoformat(),
                "history.columns": "TRADEDATE,OPENPOSITION,OPENPOSITIONVALUE",
                "start":           0,
            }
            while True:
                data   = _get(session, url, params)
                hist   = data["history"]
                cols   = hist["columns"]
                cursor = data["history.cursor"]["data"][0]
                idx, total, pagesize = cursor[0], cursor[1], cursor[2]

                for row in hist["data"]:
                    r    = dict(zip(cols, row))
                    cnt  = r.get("OPENPOSITION")
                    val  = r.get("OPENPOSITIONVALUE")
                    if not cnt and not val:
                        continue
                    d = date.fromisoformat(r["TRADEDATE"])
                    prev = totals.get(d, (0.0, 0.0))
                    totals[d] = (prev[0] + float(cnt or 0), prev[1] + float(val or 0))

                next_start = idx + pagesize
                if next_start >= total:
                    break
                params = dict(params, start=next_start)

        log.info(
            "ISS assetcode=%s: aggregated OI for %d trading days from %d contracts",
            asset_code, len(totals), len(secids),
        )
        return totals


def fetch_market_value_by_assetcode(day: date) -> dict[str, float]:
    """
    Total VALUE (roubles) per ASSETCODE for every FORTS contract on one day.

    The OKR baskets span ~58 assets, which the per-assetcode discovery above
    would turn into hundreds of ISS calls per pass.  The market-level history
    endpoint answers the same question for the WHOLE market in ~9 pages of 100
    rows, so one day costs 9 requests no matter how wide the basket is.

    Returns ``{}`` for a non-trading day — ISS publishes no rows at all for
    weekends and holidays (verified on 15–16.08.2026).  Synchronous (ISS is
    blocking) — call from a thread executor inside the async ETL.
    """
    url = f"{_ISS_BASE}/history/engines/futures/markets/forts/securities.json"
    totals: dict[str, float] = {}
    start = 0
    with _make_session() as session:
        while True:
            data = _get(session, url, {
                "date":            day.isoformat(),
                "history.columns": "SECID,ASSETCODE,VALUE",
                "start":           start,
                "limit":           _PAGESIZE,
            })
            block = data.get("history", {})
            rows  = block.get("data") or []
            if not rows:
                break
            cols = block["columns"]
            for r in rows:
                row = dict(zip(cols, r))
                code = row.get("ASSETCODE")
                if not code:
                    continue
                totals[code] = totals.get(code, 0.0) + float(row.get("VALUE") or 0)
            start += len(rows)
            if len(rows) < _PAGESIZE:
                break
    return totals
