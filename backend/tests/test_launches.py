"""Launches endpoint helpers: listing-date extraction and row ordering."""

from app.api.routes.launches import (
    _collapse_to_earliest,
    _extract_listed_at,
    _first_volume_date,
    _sort_rows,
)


def test_extract_listed_at_converts_ms_to_iso_date():
    # 1700000000000 ms = 2023-11-14 UTC
    assert _extract_listed_at({"onboardDate": 1_700_000_000_000}) == "2023-11-14"


def test_extract_listed_at_field_priority():
    # onboardDate (Binance) wins over listTime (OKX) when both present
    info = {"onboardDate": 1_700_000_000_000, "listTime": 1_600_000_000_000}
    assert _extract_listed_at(info) == "2023-11-14"


def test_extract_listed_at_skips_unparseable_and_falls_through():
    info = {"onboardDate": "not-a-number", "listTime": 1_700_000_000_000}
    assert _extract_listed_at(info) == "2023-11-14"


def test_extract_listed_at_none_when_absent():
    assert _extract_listed_at({}) is None
    assert _extract_listed_at({"onboardDate": 0}) is None   # falsy → skipped


def test_first_volume_date_skips_zero_volume_padding():
    # Hyperliquid pads old markets with empty bars before the first trade.
    # 1700006400000 ms = 2023-11-15 UTC
    bars = [
        [1_699_920_000_000, 1, 1, 1, 1, 0],
        [1_700_006_400_000, 1, 1, 1, 1, 12.5],
    ]
    assert _first_volume_date(bars) == "2023-11-15"


def test_first_volume_date_none_when_nothing_traded():
    assert _first_volume_date([[1_700_006_400_000, 1, 1, 1, 1, 0]]) is None
    assert _first_volume_date([]) is None


def test_collapse_to_earliest_keeps_first_listing_per_base():
    # Same underlying on two Hyperliquid builder DEXs → the earlier one wins.
    rows = [
        {"base": "GOLD", "symbol": "XYZ-GOLD/USDC:USDC", "listed_at": "2025-12-22"},
        {"base": "GOLD", "symbol": "FLX-GOLD/USDH:USDH", "listed_at": "2025-12-12"},
        {"base": "VIX",  "symbol": "XYZ-VIX/USDC:USDC",  "listed_at": None},
        {"base": "TSLA", "symbol": "KM-TSLA/USDH:USDH",  "listed_at": None},
        {"base": "TSLA", "symbol": "XYZ-TSLA/USDC:USDC", "listed_at": "2025-11-13"},
    ]
    out = {r["base"]: r for r in _collapse_to_earliest(rows)}
    assert len(out) == 3
    assert out["GOLD"]["symbol"] == "FLX-GOLD/USDH:USDH"
    # a dated row beats an undated one regardless of input order
    assert out["TSLA"]["symbol"] == "XYZ-TSLA/USDC:USDC"
    assert out["VIX"]["listed_at"] is None


def test_sort_rows_newest_first_undated_last():
    rows = [
        {"base": "XAU",  "listed_at": "2026-01-01"},
        {"base": "DELL", "listed_at": "2026-07-01"},
        {"base": "SPY",  "listed_at": None},           # Hyperliquid: no date
        {"base": "IBM",  "listed_at": "2026-07-01"},
        {"base": "AAPL", "listed_at": None},
    ]
    _sort_rows(rows)
    assert [r["base"] for r in rows] == ["DELL", "IBM", "XAU", "AAPL", "SPY"]
    # dated rows strictly before undated ones
    dated = [r["listed_at"] is not None for r in rows]
    assert dated == sorted(dated, reverse=True)
