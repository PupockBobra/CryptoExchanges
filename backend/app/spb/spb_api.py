"""
Client for СПБ Биржа's own public market-data API (https://spbexchange.ru/api).

Unlike the Finam path, this is the exchange's first-party feed: no auth, no
token, and it exposes exact daily turnover AND open interest per instrument.

Two endpoints are used:

    GET /im/v1/tradingResults/futuresDay/all?date=YYYY-MM-DD&page=0&size=500
    GET /stream-service/v1/funding/indicativeFunding

The first is the daily results feed (turnover + open interest), the second the
funding feed that also drives the ticker on the exchange's home page.

It is Spring-paginated (needs date + page + size, else 500) and returns one row
per instrument × trading session.  СПБ runs three sessions a day — morning (2),
main (1) and evening (3); the evening row carries the cumulative end-of-day
figures (verified: its totalQty equals Finam's daily bar volume), so callers keep
session == 3.

The site serves an incomplete TLS chain (curl needs -k), so verify=False here —
acceptable for a public read-only market-data GET.
"""

import asyncio
import logging
from datetime import date

import httpx

log = logging.getLogger(__name__)

_BASE = "https://spbexchange.ru/api"
_EOD_SESSION = 3          # evening session = cumulative end-of-day snapshot
_PAGE_SIZE = 500          # ~120 rows/day (all instruments × 3 sessions) → one page


class SpbApiClient:
    """Async client for the СПБ Биржа daily futures results endpoint.

    Use as an async context manager so the connection pool is closed:

        async with SpbApiClient() as api:
            rows = await api.fetch_futures_day_eod(date.today())
    """

    def __init__(self):
        self._http = httpx.AsyncClient(base_url=_BASE, timeout=30.0, verify=False)

    async def __aenter__(self) -> "SpbApiClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict, retries: int = 3) -> dict | list:
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                r = await self._http.get(path, params=params)
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001 — retry any transient failure
                last_exc = exc
                log.warning("SPB API GET %s failed (attempt %d/%d): %s", path, attempt + 1, retries, exc)
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
        raise RuntimeError(f"SPB API GET {path} failed after {retries} attempts: {last_exc}")

    async def fetch_futures_day_eod(self, day: date) -> list[dict]:
        """
        End-of-day (session=3) result rows for every futures instrument on ``day``.

        Returns the raw dicts (keys: futuresCode, totalOpenPosition,
        totalOpenPositionVolume, totalVolume, totalQty, …).  Empty on
        weekends/holidays (the endpoint returns an empty page).
        """
        data = await self._get(
            "/im/v1/tradingResults/futuresDay/all",
            params={"date": day.isoformat(), "page": 0, "size": _PAGE_SIZE},
        )
        content = data.get("content", []) if isinstance(data, dict) else []
        return [r for r in content if r.get("session") == _EOD_SESSION]

    async def fetch_indicative_funding(self) -> list[dict]:
        """
        Funding for every perp, as broadcast on the exchange's home page.

        One record per instrument (25 — exactly the app's SPB universe), with
        ``fundingPerContract.value`` in USD per contract and the funding period
        in ``instrumentApiDescription.periodStart/periodFinish``.

        ⚠️ Only the *current* value is served: the settled figure is published
        from 23:00 МСК (stock perps) / 00:00 (crypto index perps) until 11:30 the
        next morning, and is zeroed outside that window.  There is no history
        endpoint, so this cannot be backfilled — see app/spb/funding_exchange.py.
        """
        data = await self._get("/stream-service/v1/funding/indicativeFunding", params={})
        return data if isinstance(data, list) else []
