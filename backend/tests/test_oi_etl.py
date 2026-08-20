"""Open-interest extraction helpers (oi/etl.py).

Each exchange returns OI in a different shape (CLAUDE.md documents the
per-exchange quirks); these pin the normalization paths.
"""

from datetime import datetime, timezone

from app.oi.etl import _extract_oi_from_result, _parse_ts, _safe_float, _to_mexc_sym


def test_extract_standard_result():
    oi, oi_val = _extract_oi_from_result(
        {"openInterest": 123.0, "openInterestValue": 456.0, "info": {}}, "binance"
    )
    assert (oi, oi_val) == (123.0, 456.0)


def test_extract_falls_back_to_raw_info():
    # Bybit linear: ccxt normalization returns openInterest=None; the raw
    # info dict carries the value as a string.
    oi, oi_val = _extract_oi_from_result(
        {"openInterest": None, "openInterestValue": None,
         "info": {"openInterest": "42.5"}}, "bybit"
    )
    assert oi == 42.5
    assert oi_val is None


def test_extract_hyperliquid_computes_usd_from_mark_price():
    oi, oi_val = _extract_oi_from_result(
        {"openInterest": 100.0, "openInterestValue": None,
         "info": {"markPx": "2.5"}}, "hyperliquid"
    )
    assert oi == 100.0
    assert oi_val == 250.0


def test_extract_mark_price_only_used_for_hyperliquid():
    oi, oi_val = _extract_oi_from_result(
        {"openInterest": 100.0, "openInterestValue": None,
         "info": {"markPx": "2.5"}}, "binance"
    )
    assert oi_val is None   # binance USD value comes from the ticker instead


def test_extract_survives_okx_info_as_list():
    # OKX daily history returns info as a raw list, not a dict — must not crash.
    oi, oi_val = _extract_oi_from_result(
        {"openInterest": None, "openInterestValue": None,
         "info": ["ts", "oiUsd", "oiVol"]}, "okx"
    )
    assert (oi, oi_val) == (None, None)


def test_to_mexc_sym():
    assert _to_mexc_sym("BTC/USDT:USDT") == "BTC_USDT"
    assert _to_mexc_sym("XAU/USDT:USDT") == "XAU_USDT"


def test_safe_float_edge_cases():
    assert _safe_float("1.5") == 1.5
    assert _safe_float(None) is None
    assert _safe_float("abc") is None


def test_parse_ts():
    assert _parse_ts(None) is None
    assert _parse_ts("garbage") is None
    assert _parse_ts(1_700_000_000_000) == datetime.fromtimestamp(
        1_700_000_000, tz=timezone.utc
    )
