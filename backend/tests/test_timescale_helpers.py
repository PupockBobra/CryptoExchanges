"""Pure helpers in db/timescale.py: interval parsing, symbol formatting,
asset-class routing for the Custom Report tree."""

from datetime import timedelta

import pytest

from app.db.timescale import _crypto_class, _fmt_symbol, _parse_interval


def test_parse_interval_units():
    assert _parse_interval("1 minute") == timedelta(minutes=1)
    assert _parse_interval("5 minutes") == timedelta(minutes=5)
    assert _parse_interval("30 seconds") == timedelta(seconds=30)
    assert _parse_interval("2 hours") == timedelta(hours=2)
    assert _parse_interval("1 day") == timedelta(days=1)


def test_parse_interval_normalizes_case_and_whitespace():
    assert _parse_interval("  1 Minute ") == timedelta(minutes=1)


@pytest.mark.parametrize("bad", ["", "fortnight", "1 week", "minute", "1; DROP TABLE x"])
def test_parse_interval_rejects_garbage(bad):
    with pytest.raises(ValueError):
        _parse_interval(bad)


def test_fmt_symbol_matches_frontend_formatting():
    assert _fmt_symbol("BTC/USDT") == "BTC/USDT"
    assert _fmt_symbol("XAU/USDT:USDT") == "XAU/USDT PERP"
    # quote is normalized to USDT even for USDC-margined perps (display only)
    assert _fmt_symbol("BTC/USDC:USDC") == "BTC/USDT PERP"


def test_crypto_class_routing():
    assert _crypto_class("BRN/USDT:USDT") == "Commodities"
    assert _crypto_class("XAU/USDT:USDT") == "Commodities"
    assert _crypto_class("QQQ/USDT:USDT") == "Indexes"
    assert _crypto_class("SAMSUNG/USDT:USDT") == "Korean market"
    assert _crypto_class("NVDA/USDT:USDT") == "US stocks"
    assert _crypto_class("BTC/USDT") == "Crypto"


def test_crypto_class_routes_equity_perps_out_of_the_crypto_branch():
    # Open-interest rows name stock perps like any other perp; only the stock
    # ETL's ticker list can tell them apart from an altcoin.
    equity = {"AVGO", "MRVL"}
    assert _crypto_class("AVGO/USDT:USDT", equity) == "US stocks"
    assert _crypto_class("AVGO/USDT:USDT") == "Crypto"          # without the list
    assert _crypto_class("SOL/USDT", equity) == "Crypto"        # crypto stays crypto
    # bare equity-perp ticker (from stock_daily_volume) has no slash
    assert _crypto_class("AMD") == "US stocks"
