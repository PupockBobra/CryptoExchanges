"""
Finam TradeAPI client for SPB Exchange perpetual futures.

Auth is two-step: the long-lived secret token (``tapi_sk_…``) is exchanged for a
short-lived JWT (``tapi_ak_…``, ~15 min) via ``POST /v1/sessions``; the JWT then
goes in the ``Authorization`` header.  We cache the JWT and refresh it lazily.

Finam rate-limits rapid sequential calls aggressively (connection resets / empty
bodies), so ``_get`` retries with exponential backoff and the ETL throttles
between tickers.

The token grants read access to the owner's brokerage account — it MUST stay on
the backend (``settings.finam_api_token`` from ``.env``) and never reach the
frontend.
"""

import asyncio
import logging
import urllib.parse

import httpx

from app.spb.config import FINAM_MIC

log = logging.getLogger(__name__)

_FINAM_BASE = "https://api.finam.ru"
_JWT_TTL_SEC = 14 * 60   # refresh a minute before the ~15-min server expiry


def num_value(obj: dict, key: str) -> float:
    """Extract a float from Finam's nested ``{"value": "…"}`` fields. 0.0 if absent."""
    v = obj.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class FinamClient:
    """Async Finam client with JWT caching, retries and an httpx session.

    Use as an async context manager so the underlying connection pool is closed:

        async with FinamClient(secret) as client:
            bars = await client.fetch_daily_bars("AMZNperpA", d0, d1)
    """

    def __init__(self, secret: str):
        self._secret = secret
        self._jwt: str | None = None
        self._jwt_exp: float = 0.0
        self._http = httpx.AsyncClient(base_url=_FINAM_BASE, timeout=30.0)

    async def __aenter__(self) -> "FinamClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._http.aclose()

    async def _ensure_token(self) -> str:
        now = asyncio.get_event_loop().time()
        if self._jwt and now < self._jwt_exp:
            return self._jwt
        r = await self._http.post("/v1/sessions", json={"secret": self._secret})
        r.raise_for_status()
        self._jwt = r.json()["token"]
        self._jwt_exp = now + _JWT_TTL_SEC
        return self._jwt

    async def _get(self, path: str, params: dict | None = None, retries: int = 4) -> dict:
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                token = await self._ensure_token()
                r = await self._http.get(path, params=params, headers={"Authorization": token})
                if r.status_code == 401:
                    self._jwt = None          # force re-auth on next attempt
                    raise RuntimeError("401 unauthorized")
                r.raise_for_status()
                return r.json()
            except Exception as exc:           # noqa: BLE001 — retry any transient failure
                last_exc = exc
                log.warning("Finam GET %s failed (attempt %d/%d): %s", path, attempt + 1, retries, exc)
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
        raise RuntimeError(f"Finam GET {path} failed after {retries} attempts: {last_exc}")

    def _symbol(self, ticker: str, mic: str | None = None) -> str:
        return urllib.parse.quote(f"{ticker}@{mic or FINAM_MIC}")

    async def fetch_daily_bars(self, ticker: str, from_date, till_date) -> list[dict]:
        """Daily OHLCV bars in [from_date, till_date].  Each bar carries volume (contracts)."""
        data = await self._get(
            f"/v1/instruments/{self._symbol(ticker)}/bars",
            params={
                "timeframe": "TIME_FRAME_D",
                "interval.start_time": f"{from_date.isoformat()}T00:00:00Z",
                "interval.end_time": f"{till_date.isoformat()}T23:59:59Z",
            },
        )
        return data.get("bars", [])

    async def fetch_latest_quote(self, ticker: str) -> dict:
        """Live quote for the current session — carries exact ``volume`` and ``turnover`` (USD)."""
        data = await self._get(f"/v1/instruments/{self._symbol(ticker)}/quotes/latest")
        return data.get("quote", {})

    async def fetch_orderbook(self, ticker: str, mic: str | None = None, retries: int = 4) -> dict:
        """
        Live order-book snapshot.  ``rows`` each carry ``price`` plus one of
        ``buy_size`` (bid) / ``sell_size`` (ask), price in USD, size in contracts.

        ``mic`` overrides the default MIC (SPB perps under ``RUSX``); pass
        ``"RTSX"`` with a FORTS SECID for MOEX derivatives.

        The continuous poller passes ``retries=1``: retrying a rate-limited (429)
        call with backoff only amplifies load, so it skips and waits for the next
        cycle instead.
        """
        data = await self._get(f"/v1/instruments/{self._symbol(ticker, mic)}/orderbook", retries=retries)
        return data.get("orderbook", {})
