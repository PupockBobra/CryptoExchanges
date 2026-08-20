"""Columnar reshape behind /api/history/hourly-{volume,profile}."""

from app.api.routes.history import pivot_to_series


def row(axis, symbol, exchange, value):
    return {"axis": axis, "symbol": symbol, "exchange": exchange, "value": value}


AXIS_OF  = lambda r: r["axis"]    # noqa: E731
VALUE_OF = lambda r: r["value"]   # noqa: E731


def test_axis_is_derived_in_first_seen_order():
    rows = [row("h1", "BTC", "binance", 1.0), row("h2", "BTC", "binance", 2.0)]
    axis, series = pivot_to_series(rows, AXIS_OF, VALUE_OF)
    assert axis == ["h1", "h2"]
    assert series == [{"symbol": "BTC", "exchange": "binance", "values": [1.0, 2.0]}]


def test_one_series_per_symbol_exchange_pair():
    rows = [
        row("h1", "BTC", "binance", 1.0),
        row("h1", "BTC", "okx",     2.0),
        row("h1", "ETH", "binance", 3.0),
    ]
    axis, series = pivot_to_series(rows, AXIS_OF, VALUE_OF)
    assert axis == ["h1"]
    assert len(series) == 3
    assert {(s["symbol"], s["exchange"]) for s in series} == {
        ("BTC", "binance"), ("BTC", "okx"), ("ETH", "binance"),
    }


def test_missing_points_are_none_not_zero():
    """A pair listed midway through the window has no bar — that is not 0 volume."""
    rows = [row("h1", "BTC", "binance", 1.0), row("h3", "BTC", "binance", 3.0)]
    axis, series = pivot_to_series(rows, AXIS_OF, VALUE_OF, axis=["h1", "h2", "h3"])
    assert axis == ["h1", "h2", "h3"]
    assert series[0]["values"] == [1.0, None, 3.0]


def test_pinned_axis_keeps_empty_categories():
    """The profile always renders all 24 hours, even the ones with no trading."""
    rows = [row(9, "BTC", "binance", 5.0)]
    axis, series = pivot_to_series(rows, AXIS_OF, VALUE_OF, axis=list(range(24)))
    assert axis == list(range(24))
    values = series[0]["values"]
    assert len(values) == 24
    assert values[9] == 5.0
    assert all(v is None for i, v in enumerate(values) if i != 9)


def test_rows_outside_a_pinned_axis_are_dropped():
    rows = [row(9, "BTC", "binance", 5.0), row(99, "BTC", "binance", 7.0)]
    _axis, series = pivot_to_series(rows, AXIS_OF, VALUE_OF, axis=list(range(24)))
    assert series[0]["values"][9] == 5.0
    assert 7.0 not in series[0]["values"]


def test_empty_input():
    assert pivot_to_series([], AXIS_OF, VALUE_OF) == ([], [])
    assert pivot_to_series([], AXIS_OF, VALUE_OF, axis=[1, 2]) == ([1, 2], [])
