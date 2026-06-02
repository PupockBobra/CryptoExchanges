import logging
from datetime import datetime, timezone

from app.db.timescale import insert_alert
from app.config import settings
from app.models.arbitrage import ArbitrageAlert
from app.redis_client import get_redis

log = logging.getLogger(__name__)

# In-memory latest ask/bid per (exchange, symbol). Each entry carries its own
# arrival timestamp so stale entries (from a degraded WS) can be filtered.
_latest: dict[tuple[str, str], dict] = {}

# Cooldown state: tracks the last-fired alert per (symbol, buy_ex, sell_ex)
# Value is (last_fired_ts, last_spread_pct)
_last_alert: dict[tuple[str, str, str], tuple[float, float]] = {}

# Re-fire an alert for the same opportunity only after this many seconds,
# OR if the spread has shifted by more than SPREAD_RETRIGGER_PCT percentage points.
ALERT_COOLDOWN_SECS   = 30
SPREAD_RETRIGGER_PCT  = 0.05   # 0.05 pp change re-triggers immediately

# Ignore quotes older than this — prevents false signals when one exchange's
# WebSocket is hung and its last known price drifts away from the live market.
STALE_TICK_SECS = 10


async def on_tick(exchange: str, symbol: str, bid: float, ask: float, last: float):
    """Called by collectors on every price update."""
    _latest[(exchange, symbol)] = {
        "bid": bid, "ask": ask, "last": last,
        "ts":  datetime.now(timezone.utc).timestamp(),
    }
    await _check_arbitrage(symbol)


async def _check_arbitrage(symbol: str):
    now = datetime.now(timezone.utc).timestamp()
    ticks = {
        ex: t
        for (ex, sym), t in _latest.items()
        if sym == symbol and (now - t["ts"]) <= STALE_TICK_SECS
    }

    if len(ticks) < 2:
        return

    best_bid_ex = max(ticks, key=lambda ex: ticks[ex]["bid"])
    best_ask_ex = min(ticks, key=lambda ex: ticks[ex]["ask"])

    if best_bid_ex == best_ask_ex:
        return

    sell_price = ticks[best_bid_ex]["bid"]
    buy_price = ticks[best_ask_ex]["ask"]
    spread_pct = (sell_price - buy_price) / buy_price * 100

    if spread_pct < settings.arbi_threshold_pct:
        return

    # ── Deduplication: suppress if same opportunity fired recently ───────────
    key = (symbol, best_ask_ex, best_bid_ex)
    last_ts, last_spread = _last_alert.get(key, (0.0, 0.0))

    time_ok   = (now - last_ts) >= ALERT_COOLDOWN_SECS
    spread_ok = abs(spread_pct - last_spread) >= SPREAD_RETRIGGER_PCT

    if not time_ok and not spread_ok:
        return   # same opportunity, within cooldown, spread hasn't moved enough

    _last_alert[key] = (now, spread_pct)
    # ─────────────────────────────────────────────────────────────────────────

    alert = ArbitrageAlert(
        symbol=symbol,
        buy_exchange=best_ask_ex,
        sell_exchange=best_bid_ex,
        buy_price=buy_price,
        sell_price=sell_price,
        spread_pct=round(spread_pct, 4),
        ts=datetime.now(timezone.utc),
    )
    log.info("Arbitrage %s spread=%.4f%%", symbol, spread_pct)

    r = await get_redis()
    await r.publish("arbitrage:alerts", alert.model_dump_json())
    await insert_alert(symbol, best_ask_ex, best_bid_ex, buy_price, sell_price, spread_pct)
