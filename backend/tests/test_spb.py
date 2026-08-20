"""SPB config invariants + Finam field parsing.

Regression target (CLAUDE.md): the crypto-index lot multipliers — without
them the backfill turnover is inflated thousands of times (turnover ≈
volume × typical price × lot). Lots were verified empirically against live
quotes; pin them.
"""

from app.spb.config import SPB_GROUPS, SPB_INSTRUMENTS, SPB_LOTS, SPB_NAMES, SPB_TICKERS
from app.spb.fetcher import num_value
from app.spb.oi_etl import _CODE_TO_TICKER, _num


def test_crypto_index_lots_pinned():
    assert SPB_LOTS["BTCUSDperpA"] == 0.0001
    assert SPB_LOTS["ETHUSDperpA"] == 0.001
    assert SPB_LOTS["SOLUSDperpA"] == 0.1
    assert SPB_LOTS["XRPUSDperpA"] == 10.0
    assert SPB_LOTS["TRXUSDperpA"] == 10.0


def test_all_us_market_perps_are_lot_one():
    for ticker, (_name, lot, group) in SPB_INSTRUMENTS.items():
        if group == "US Market":
            assert lot == 1.0, f"{ticker} lot changed from 1.0"


def test_derived_lookups_cover_every_instrument():
    assert len(SPB_TICKERS) == 25
    assert set(SPB_NAMES) == set(SPB_LOTS) == set(SPB_GROUPS) == set(SPB_INSTRUMENTS)


def test_oi_code_mapping_strips_trailing_a():
    # СПБ's daily-results feed drops the trailing "A" of the Finam ticker.
    assert _CODE_TO_TICKER["BTCUSDperp"] == "BTCUSDperpA"
    assert _CODE_TO_TICKER["AMZNperp"] == "AMZNperpA"
    assert len(_CODE_TO_TICKER) == len(SPB_TICKERS)
    for code, ticker in _CODE_TO_TICKER.items():
        assert ticker == code + "A"


def test_num_value_parses_finam_nested_and_flat_fields():
    assert num_value({"volume": {"value": "1.5"}}, "volume") == 1.5
    assert num_value({"volume": "2"}, "volume") == 2.0
    assert num_value({"volume": 3}, "volume") == 3.0
    # Days with no trades omit the field entirely → 0, not a crash
    assert num_value({}, "volume") == 0.0
    assert num_value({"volume": None}, "volume") == 0.0
    assert num_value({"volume": {"value": ""}}, "volume") == 0.0
    assert num_value({"volume": "abc"}, "volume") == 0.0


def test_oi_num_helper():
    assert _num("5.5") == 5.5
    assert _num(None) == 0.0
    assert _num("x") == 0.0
