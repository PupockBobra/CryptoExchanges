"""
MOEX ISS history fetcher.

Uses curl_cffi (Chrome TLS fingerprint) to bypass ISS's JA3-based filtering.
Set MOEX_ISS_PROXY env var (e.g. socks5://user:pass@host:port) for environments
where the server IP is geo-blocked by MOEX.

All public functions return plain Python dicts — no async, so they can be called
from a ThreadPoolExecutor inside the async ETL loop without blocking the event loop.
"""

import json
import logging
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


def aggregate_asset_value(
    asset_secids: list[str],
    from_date: date,
    till_date: date,
) -> dict[date, float]:
    """
    Fetch history for every SECID in asset_secids, sum VALUE by date.
    Returns {trade_date: total_value_rub}.
    Missing VALUE (None / 0) rows are skipped.
    """
    totals: dict[date, float] = {}
    with _make_session() as session:
        for secid in asset_secids:
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
                    d = dict(zip(cols, row))
                    val = d.get("VALUE")
                    if not val:
                        continue
                    trade_date = date.fromisoformat(d["TRADEDATE"])
                    totals[trade_date] = totals.get(trade_date, 0.0) + float(val)

                next_start = idx + pagesize
                if next_start >= total:
                    break
                params = dict(params, start=next_start)

    return totals


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
