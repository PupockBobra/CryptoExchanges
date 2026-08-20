"""
AVG_SPREAD (spread-on-volume) math — `avg_spread_on_volume` / `_vwap_to_notional`.

The metric: walk each side of the book filling `target` roubles of notional per
side (target on the bid, target on the ask), take the volume-weighted average
fill price, return the USD price gap (P_aver_ask - P_aver_bid) — NOT multiplied
by the lot.  None when a side lacks `target` of depth.
"""

import pytest

from app.spb.orderbook import _vwap_to_notional, avg_spread_on_volume, spread_pct_on_volume


def _levels(pairs):
    return [{"price": p, "size": s} for p, s in pairs]


def test_vwap_single_level_partial_fill():
    # One level 100@10, lot 1, usdrub 1 → 1000 RUB available. Fill 500 → 5 contracts.
    vwap = _vwap_to_notional(_levels([(100.0, 10.0)]), lot=1.0, usdrub=1.0, target_rub=500.0)
    assert vwap == pytest.approx(100.0)


def test_vwap_walks_multiple_levels():
    # Fill 1500 RUB across 100@10 (=1000) then 101@10. Need 500 more from L2 → ~4.9505 contr.
    # VWAP = (100*10 + 101*4.9505) / (10 + 4.9505)
    levels = _levels([(100.0, 10.0), (101.0, 10.0)])
    vwap = _vwap_to_notional(levels, lot=1.0, usdrub=1.0, target_rub=1500.0)
    need_qty = 500.0 / (101.0 * 1.0 * 1.0)
    expected = (100.0 * 10.0 + 101.0 * need_qty) / (10.0 + need_qty)
    assert vwap == pytest.approx(expected)


def test_vwap_insufficient_depth_returns_none():
    # Only 1000 RUB in the book, need 2000.
    assert _vwap_to_notional(_levels([(100.0, 10.0)]), 1.0, 1.0, 2000.0) is None


def test_spread_symmetric_book():
    # Bids at 99 (best) / 98, asks at 101 (best) / 102, deep enough for 1000/side.
    bids = _levels([(99.0, 100.0), (98.0, 100.0)])
    asks = _levels([(101.0, 100.0), (102.0, 100.0)])
    # 1000 RUB/side fills entirely within the best level → P_aver_ask=101, P_aver_bid=99.
    sp = avg_spread_on_volume(bids, asks, lot=1.0, usdrub=1.0, target_rub=1000.0)
    assert sp == pytest.approx(2.0)


def test_spread_is_lot_independent():
    # The absolute spread is the raw USD price gap — it does NOT scale with the
    # lot (lot only sizes the rouble fill depth).  Same gap for any lot.
    bids = _levels([(99.0, 1_000_000.0)])
    asks = _levels([(101.0, 1_000_000.0)])
    a = avg_spread_on_volume(bids, asks, lot=0.0001, usdrub=1.0, target_rub=1000.0)
    b = avg_spread_on_volume(bids, asks, lot=100.0,  usdrub=1.0, target_rub=1000.0)
    assert a == pytest.approx(2.0) and b == pytest.approx(2.0)


def test_spread_none_if_one_side_thin():
    bids = _levels([(99.0, 1.0)])                 # only ~99 RUB on the bid
    asks = _levels([(101.0, 1_000_000.0)])
    assert avg_spread_on_volume(bids, asks, 1.0, 1.0, 1000.0) is None


def test_spread_pct_symmetric_book():
    # P_aver_ask=101, P_aver_bid=99, mid=100 → (101-99)/100*100 = 2.0 %.
    bids = _levels([(99.0, 100.0), (98.0, 100.0)])
    asks = _levels([(101.0, 100.0), (102.0, 100.0)])
    pct = spread_pct_on_volume(bids, asks, lot=1.0, usdrub=1.0, target_rub=1000.0)
    assert pct == pytest.approx(2.0)


def test_spread_pct_is_unit_free():
    # lot and usdrub cancel in the ratio → same % regardless of their values.
    bids = _levels([(99.0, 1_000_000.0)])
    asks = _levels([(101.0, 1_000_000.0)])
    a = spread_pct_on_volume(bids, asks, lot=0.0001, usdrub=1.0,  target_rub=1000.0)
    b = spread_pct_on_volume(bids, asks, lot=1.0,    usdrub=90.0, target_rub=1000.0)
    assert a == pytest.approx(b) == pytest.approx((101.0 - 99.0) / 100.0 * 100.0)


def test_spread_pct_uses_top_of_book_mid_not_fill_mid():
    # Best bid 99, best ask 101 → top-of-book mid = 100.  The ask fill walks past
    # a tiny best level (101@1) into a deep 110 level, so P_aver_ask >> 101 and the
    # averaged-fill mid (P_aver_ask+P_aver_bid)/2 would be ~104, not 100.  The pct
    # must divide by the top-of-book mid (100), so it doesn't shrink from that.
    bids = _levels([(99.0, 1_000.0)])
    asks = _levels([(101.0, 1.0), (110.0, 1_000.0)])
    p_ask = 1000.0 / (1.0 + 899.0 / 110.0)   # VWAP to fill 1000 RUB on the ask
    expected = (p_ask - 99.0) / 100.0 * 100.0
    pct = spread_pct_on_volume(bids, asks, lot=1.0, usdrub=1.0, target_rub=1000.0)
    assert pct == pytest.approx(expected)


def test_spread_pct_none_if_one_side_thin():
    bids = _levels([(99.0, 1.0)])
    asks = _levels([(101.0, 1_000_000.0)])
    assert spread_pct_on_volume(bids, asks, 1.0, 1.0, 1000.0) is None


# ── wall-clock sweeps → plain 15-minute averages ──────────────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

from app.spb.spread_etl import (  # noqa: E402
    _BUCKET_SEC,
    _add_sample,
    _avg_samples,
    _bucket_start,
    _fresh_acc,
    _is_trading_now,
    _mean,
    _next_minute,
)


def _dt(h, m, s=0):
    return datetime(2026, 7, 9, h, m, s, tzinfo=timezone.utc)


def test_next_minute_is_epoch_aligned():
    # Strictly after `now`, pinned to :00 of every minute — the same grid on
    # every instance, which is what makes cross-host averages comparable.
    assert _next_minute(_dt(10, 0, 1)) == _dt(10, 1)
    assert _next_minute(_dt(10, 0, 59)) == _dt(10, 1)
    assert _next_minute(_dt(10, 1)) == _dt(10, 2)   # exactly on the mark → next one


def test_bucket_start_snaps_to_15_min_grid():
    assert _bucket_start(_dt(10, 0)) == _dt(10, 0)
    assert _bucket_start(_dt(10, 14, 59)) == _dt(10, 0)
    assert _bucket_start(_dt(10, 29, 1)) == _dt(10, 15)


def test_flush_labels_interval_end():
    # The 17:45 point is the average over 17:30–17:45 — rows carry the interval
    # END, so a point appears on the chart at its own labeled time.
    from app.moex.config import MOEX_CRYPTO_FUTURES
    from app.spb.config import SPB_TICKERS
    from app.spb.spread_etl import _flush

    spb_acc = {t: _fresh_acc() for t in SPB_TICKERS}
    moex_acc = {t: _fresh_acc() for t in MOEX_CRYPTO_FUTURES}
    first = SPB_TICKERS[0]
    _add_sample(spb_acc[first], {"1m_usd": 2.0, "10m_usd": None, "1m_pct": 0.1, "10m_pct": None})
    rows, moex_rows = _flush(_dt(10, 30), spb_acc, moex_acc)
    assert rows[0][0] == _dt(10, 45)
    assert rows[0][1] == first
    assert rows[0][2] == pytest.approx(2.0)
    assert all(r[0] == _dt(10, 45) for r in moex_rows)


def test_next_minute_honours_a_coarser_grid():
    # MM cannot sweep ~100 instruments inside a minute, so it samples on the
    # 120 s grid.  Still epoch-aligned: both hosts hit the same instants.
    assert _next_minute(_dt(10, 0, 1), 120) == _dt(10, 2)
    assert _next_minute(_dt(10, 1, 30), 120) == _dt(10, 2)
    assert _next_minute(_dt(10, 2), 120) == _dt(10, 4)


def test_120s_grid_alternates_8_and_7_samples_per_bucket():
    # 900 s isn't a multiple of 120 s, so consecutive 15-min buckets get 8 and 7
    # samples (15 per half hour) — the figure the chart's averages rest on.
    counts, t = [], _dt(10, 0)
    for _ in range(2):
        bucket, n = _bucket_start(t), 0
        while _bucket_start(t) == bucket:
            n += 1
            t = _next_minute(t, 120)
        counts.append(n)
    assert counts == [8, 7]


def test_avg_samples_reports_bucket_depth():
    accs = {"A": _fresh_acc(), "B": _fresh_acc()}
    for v in (1.0, 2.0, 3.0):
        _add_sample(accs["A"], {"1m_usd": v})
    _add_sample(accs["B"], {"1m_usd": 1.0})
    assert _avg_samples(accs, "1m_usd") == pytest.approx(2.0)   # (3 + 1) / 2
    assert _avg_samples({}, "1m_usd") == 0.0


# ── trading-hours gate ────────────────────────────────────────────────────────

def _msk(y, mo, d, h, m):
    """UTC instant for a Moscow wall-clock time (MSK = UTC+3, no DST)."""
    return datetime(y, mo, d, h, m, tzinfo=timezone(timedelta(hours=3)))


def test_trading_gate_weekday_bounds():
    thu = lambda h, m: _msk(2026, 7, 9, h, m)   # noqa: E731 — Thursday
    assert not _is_trading_now(thu(6, 59))
    assert _is_trading_now(thu(7, 0))
    assert _is_trading_now(thu(23, 44))         # FORTS evening session
    assert not _is_trading_now(thu(23, 45))


def test_trading_gate_weekend_bounds():
    sat = lambda h, m: _msk(2026, 7, 25, h, m)  # noqa: E731
    assert not _is_trading_now(sat(9, 59))
    assert _is_trading_now(sat(10, 0))
    assert _is_trading_now(sat(18, 59))
    assert not _is_trading_now(sat(19, 0))
    assert not _is_trading_now(_msk(2026, 7, 26, 22, 0))   # Sunday night


def test_last_sampled_minute_is_labeled_inside_the_window():
    # The row label is the interval END, so the last sampled minute must sit in a
    # bucket whose end is still inside the trading window — otherwise the point
    # is written with a label the frontend's rangebreaks cut off the x-axis.
    # This is why the weekday gate closes at 23:45 and not at the 23:50 the
    # evening session actually runs to.
    for day, close_h, close_m in [(9, 23, 45), (25, 19, 0)]:
        last = _msk(2026, 7, day, close_h, close_m) - timedelta(minutes=1)
        assert _is_trading_now(last)
        label = _bucket_start(last) + timedelta(seconds=_BUCKET_SEC)
        assert label <= _msk(2026, 7, day, close_h, close_m)


def test_mean_accumulator_averages_and_skips_none():
    acc = _fresh_acc()
    _add_sample(acc, {"1m_usd": 2.0, "10m_usd": 20.0, "1m_pct": 0.2, "10m_pct": None})
    _add_sample(acc, {"1m_usd": 4.0, "10m_usd": None, "1m_pct": 0.4, "10m_pct": None})
    _add_sample(acc, {"1m_usd": 6.0, "10m_usd": 40.0, "1m_pct": 0.6, "10m_pct": None})
    assert _mean(acc, "1m_usd") == pytest.approx(4.0)     # (2+4+6)/3
    assert _mean(acc, "10m_usd") == pytest.approx(30.0)   # failed sample excluded
    assert _mean(acc, "1m_pct") == pytest.approx(0.4)
    assert _mean(acc, "10m_pct") is None                  # never had depth → no line


# ── live spread from the cached book (`compute_live_spreads`) ─────────────────

from app.spb import orderbook as ob_mod  # noqa: E402


def test_compute_live_spreads_from_cache(monkeypatch):
    # One warm book: symmetric 99/101, deep on both sides → spread = 2 USD/contract
    # for both targets.  Empty books are omitted.
    warm = dict(ob_mod._placeholder("AMDperpA"))
    warm["bids"] = _levels([(99.0, 1_000_000.0)])
    warm["asks"] = _levels([(101.0, 1_000_000.0)])
    cache = {t: ob_mod._placeholder(t) for t in ob_mod.SPB_TICKERS}
    cache["AMDperpA"] = warm
    monkeypatch.setattr(ob_mod, "_cache", cache)

    rows = ob_mod.compute_live_spreads(usdrub=1.0)   # AMD lot is 1 (US stock)
    assert [r["ticker"] for r in rows] == ["AMDperpA"]
    assert rows[0]["spread_1m_usd"] == pytest.approx(2.0)
    assert rows[0]["spread_10m_usd"] == pytest.approx(2.0)
