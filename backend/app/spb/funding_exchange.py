"""
Funding ingest from СПБ Биржа's own feed — the primary path, no Telegram.

The exchange publishes the settled funding for every perp on its home page
(``/stream-service/v1/funding/indicativeFunding``, no token), covering exactly
the 25 tickers this app tracks.  That feed carries the same per-contract figure
the @beststocks_neo channel republishes as ``Fund curr``, so the daily CSVs stop
being the only way in.

Two properties shape everything here:

* **The value lives in a window.**  The settled figure is broadcast from 23:00
  МСК (stock perps) / 00:00 (crypto index perps) until 11:30 the next morning,
  and reads 0 outside it.  So the loop only runs inside the window, and a day
  missed there is lost — the feed has no history endpoint (same "cannot be
  backfilled" property as the order book).  The channel's archive remains the
  only source for past days: ``app/spb/funding_tg.py``.
* **Percentages are derived, not published.**  The feed has no ``% day`` /
  ``% year`` / ``MeanPrice`` / ``MeanIndex``.  The channel's own numbers satisfy
  ``pct_day = fund_curr / (MeanPrice × lot) × 100`` and ``pct_year = pct_day ×
  365`` (verified against its CSV for 19-08-2026: BTC 0.00109425 / (68733.95 ×
  0.0001) = 0.01592 %, × 365 = 5.81 — both match to the last digit), so we apply
  the same formula over the funding period's typical price.  ⚠️ That price base
  is ours, not theirs, so our percentages differ from the channel's in the 3rd–4th
  digit.  ``fund_curr`` itself is exact; ``mean_index`` is simply unavailable.

Because of that, exchange-derived rows never overwrite rows that came from a
channel CSV — see ``upsert_spb_funding_from_exchange``.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db.timescale import upsert_spb_funding_from_exchange
from app.spb.config import SPB_LOTS
from app.spb.spb_api import SpbApiClient

log = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")

# Publication window (МСК): stock perps settle at 23:00, crypto at 00:00, and
# both stay readable until 11:30.  Starting at 23:00 catches the stock perps the
# same evening; the crypto rows simply read "not published" until midnight.
_WINDOW_START_HOUR = 23          # inclusive, evening of the funding day
_WINDOW_END_MINUTES = 11 * 60 + 30   # 11:30 МСК next morning

_POLL_SEC = 300      # wall-clock check, so a host suspend is recovered on resume
_REFRESH_SEC = 900   # one pass per 15 min inside the window (the value is settled)
_STARTUP_DELAY_SEC = 90

# ``flag`` bit 2 marks a genuine zero.  Without it a 0 means "not published yet"
# — the exchange's own page renders a dash there, and storing it would write a
# fake 0 %% over the whole board outside the window.
_FLAG_REAL_ZERO = 2


def in_window(now_msk: datetime) -> bool:
    """True inside the funding-publication window (23:00 → 11:30 МСК)."""
    minutes = now_msk.hour * 60 + now_msk.minute
    return now_msk.hour >= _WINDOW_START_HOUR or minutes < _WINDOW_END_MINUTES


def is_published(value: float | None, flag: int | None) -> bool:
    """Whether a record carries a settled figure rather than an empty slot."""
    if value is None:
        return False
    if value != 0:
        return True
    return isinstance(flag, int) and bool(flag & _FLAG_REAL_ZERO)


def funding_date(desc: dict) -> date | None:
    """The day the funding belongs to = the day its accrual period opened
    (``2026-08-19T19:00`` → 19-08), which is how the channel dates its files."""
    raw = desc.get("periodStart")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def typical_price(desc: dict) -> float | None:
    """(H+L+C)/3 over the funding period — the same typical-price convention the
    SPB turnover ETL uses.  Falls back to the closing print."""
    h, l, c = desc.get("phigh"), desc.get("plow"), desc.get("pclose")
    if all(isinstance(x, (int, float)) and x > 0 for x in (h, l, c)):
        return (h + l + c) / 3
    close = desc.get("priceCloseIq")
    return close if isinstance(close, (int, float)) and close > 0 else None


def build_funding_rows(payload: list[dict]) -> list[tuple]:
    """
    Feed records → rows for ``upsert_spb_funding_from_exchange``:
    ``(date, ticker, pct_year, pct_day, fund_curr, mean_price, mean_index)``.

    Records that aren't ours, aren't published yet, or lack a period are skipped.
    ``mean_index`` is always None — the feed has no index price — and doubles as
    the marker that keeps these rows from clobbering channel rows.
    """
    rows: list[tuple] = []
    for rec in payload:
        desc = rec.get("instrumentApiDescription") or {}
        ticker = desc.get("symbol")
        # ⚠️ The lot comes from SPB_LOTS, not from the feed: the feed reports
        # lot = 1.0 for the crypto index perps, while their price multiplier is
        # 0.0001…10 (without it the percentages are off by orders of magnitude).
        if ticker not in SPB_LOTS:
            continue

        value = (rec.get("fundingPerContract") or {}).get("value")
        if not is_published(value, rec.get("flag")):
            continue
        day = funding_date(desc)
        if day is None:
            continue

        price = typical_price(desc)
        lot = SPB_LOTS[ticker]
        pct_day = pct_year = None
        if price and lot:
            pct_day = value / (price * lot) * 100
            pct_year = pct_day * 365

        rows.append((day, ticker, pct_year, pct_day, float(value), price, None))
    return rows


async def ingest_funding_from_exchange() -> dict:
    """One pass: read the feed, derive rows, upsert.  Returns a small report."""
    async with SpbApiClient() as api:
        payload = await api.fetch_indicative_funding()

    rows = build_funding_rows(payload)
    saved = await upsert_spb_funding_from_exchange(rows)
    if saved:
        from app.api.cache import clear_cache
        clear_cache()          # the funding heatmap is TTL-cached

    days = sorted({str(r[0]) for r in rows})
    return {"ok": True, "records": len(payload), "rows": len(rows), "saved": saved, "days": days}


async def spb_funding_exchange_loop() -> None:
    """Poll the exchange feed inside the publication window; never raises."""
    await asyncio.sleep(_STARTUP_DELAY_SEC)
    last_run = datetime.min.replace(tzinfo=_MSK)
    while True:
        now = datetime.now(_MSK)
        if in_window(now) and (now - last_run).total_seconds() >= _REFRESH_SEC:
            last_run = now
            try:
                res = await ingest_funding_from_exchange()
                log.info(
                    "SPB funding (exchange): %d/%d record(s) published → %d row(s) saved for %s",
                    res["rows"], res["records"], res["saved"], ", ".join(res["days"]) or "—",
                )
            except Exception as e:                      # noqa: BLE001 — loop must survive
                log.warning("SPB funding (exchange) pass failed: %r", e)
        await asyncio.sleep(_POLL_SEC)


if __name__ == "__main__":
    # `check` reads the feed and prints what would be stored, without writing —
    # for verifying a morning's numbers against the channel's CSV for that day.
    import sys

    if (sys.argv[1] if len(sys.argv) > 1 else "") == "check":
        async def _check() -> None:
            async with SpbApiClient() as api:
                payload = await api.fetch_indicative_funding()
            rows = build_funding_rows(payload)
            print(f"{len(payload)} record(s), {len(rows)} published")
            print(f"{'ticker':<14}{'date':<12}{'fund_curr':>16}{'% day':>10}{'% year':>10}{'mean price':>14}")
            for day, ticker, pct_year, pct_day, fund, price, _ in sorted(rows, key=lambda r: r[1]):
                print(f"{ticker:<14}{str(day):<12}{fund:>16.10f}"
                      f"{pct_day if pct_day is None else round(pct_day, 5):>10}"
                      f"{pct_year if pct_year is None else round(pct_year, 3):>10}"
                      f"{price if price is None else round(price, 4):>14}")
        asyncio.run(_check())
    else:
        raise SystemExit("usage: python -m app.spb.funding_exchange check")
