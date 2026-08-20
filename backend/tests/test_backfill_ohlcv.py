"""parse_ohlcv_batch — the MEXC future-bar guard.

Regression target (CLAUDE.md gotcha): MEXC daily klines return placeholder
bars out to year 2063; without the guard the pagination loop writes tens of
thousands of bogus rows and explodes the hypertable chunk count.
"""

from datetime import datetime, timezone

from app.backfill.ohlcv import parse_ohlcv_batch

NOW_MS = 1_750_000_000_000  # fixed "now" for determinism


def _bar(ts_ms, o=1.0, h=2.0, low=0.5, c=1.5, vol=10.0):
    return [ts_ms, o, h, low, c, vol]


def test_future_bars_are_dropped_and_flagged():
    batch = [_bar(NOW_MS - 86_400_000), _bar(NOW_MS + 86_400_000)]
    rows, saw_future = parse_ohlcv_batch(batch, NOW_MS, "BTC/USDT", "mexc")
    assert saw_future is True
    assert len(rows) == 1
    assert rows[0][0] == datetime.fromtimestamp((NOW_MS - 86_400_000) / 1000, tz=timezone.utc)


def test_clean_batch_is_not_flagged():
    batch = [_bar(NOW_MS - 2 * 86_400_000), _bar(NOW_MS - 86_400_000)]
    rows, saw_future = parse_ohlcv_batch(batch, NOW_MS, "BTC/USDT", "binance")
    assert saw_future is False
    assert len(rows) == 2


def test_mexc_contract_size_scales_base_and_quote_volume():
    # MEXC contract klines report vol in raw contract units; contractSize=0.01
    # must scale both base and quote volume (otherwise volumes are 100× too big).
    batch = [_bar(NOW_MS - 86_400_000, c=50.0, vol=100.0)]
    rows, _ = parse_ohlcv_batch(batch, NOW_MS, "BTC/USDT", "mexc", contract_size=0.01)
    ts, canonical, exchange_id, o, h, low, c, base_vol, quote_vol = rows[0]
    assert base_vol == 1.0            # 100 contracts × 0.01
    assert quote_vol == 50.0          # 1.0 × close 50


def test_quote_volume_zero_when_close_or_volume_missing():
    batch = [_bar(NOW_MS - 86_400_000, c=0.0, vol=10.0),
             _bar(NOW_MS - 2 * 86_400_000, c=5.0, vol=0.0)]
    rows, _ = parse_ohlcv_batch(batch, NOW_MS, "X/USDT", "okx")
    assert rows[0][8] == 0.0
    assert rows[1][8] == 0.0


def test_quote_volume_rounded_to_4_decimals():
    batch = [_bar(NOW_MS - 86_400_000, c=3.33333, vol=3.0)]
    rows, _ = parse_ohlcv_batch(batch, NOW_MS, "X/USDT", "okx")
    assert rows[0][8] == round(3.33333 * 3.0, 4)


def test_row_shape_matches_upsert_contract():
    # (ts, symbol, exchange, open, high, low, close, base_volume, quote_volume)
    batch = [_bar(NOW_MS - 86_400_000)]
    rows, _ = parse_ohlcv_batch(batch, NOW_MS, "XAU/USDT:USDT", "bybit")
    row = rows[0]
    assert len(row) == 9
    assert row[1] == "XAU/USDT:USDT"
    assert row[2] == "bybit"
    assert row[0].tzinfo is not None
