"""Launches endpoint helpers: listing-date extraction and row ordering."""

from app.api.routes.launches import _extract_listed_at, _sort_rows


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
