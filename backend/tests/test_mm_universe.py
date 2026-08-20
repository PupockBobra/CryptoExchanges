"""MM universe: front-month resolution for underlyings ISS omits from its
collection listing (WTI — see MM_EXTRA_ASSETS)."""

from datetime import date, timedelta

from app.mm import universe as u


def _snap_entry(assetcode, shortname, lsttrade):
    return {"step_ratio": 786.98, "price": 84.0, "oi": 500.0,
            "assetcode": assetcode, "shortname": shortname, "lsttrade": lsttrade}


def _snapshot():
    today = date.today()
    d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    return {
        "WTQ6": _snap_entry("WTI", "WTI-8.26", d(22)),
        "WTU6": _snap_entry("WTI", "WTI-9.26", d(53)),
        "WTM6": _snap_entry("WTI", "WTI-6.26", d(-30)),     # expired
        "BRQ6": _snap_entry("BR", "BR-8.26", d(5)),         # not an extra asset
    }


def test_extra_front_month_is_nearest_unexpired(monkeypatch):
    monkeypatch.setattr(u, "_security_units", lambda s, secid: {"FACEUNIT": "USR", "UNIT": None})
    rows = u._extra_front_months(None, _snapshot(), "commodity")
    assert [(r["ASSETCODE"], r["SECID"]) for r in rows] == [("WTI", "WTQ6")]
    assert rows[0]["SHORTNAME"] == "WTI-8.26"
    assert rows[0]["FACEUNIT"] == "USR"


def test_extra_assets_only_land_in_their_own_group(monkeypatch):
    monkeypatch.setattr(u, "_security_units", lambda s, secid: {"FACEUNIT": "USR", "UNIT": None})
    assert u._extra_front_months(None, _snapshot(), "currency") == []


# ── quote unit for the absolute-spread axis (quote_symbol) ────────────────────
#
# Values below are the real ISS FACEUNIT/UNIT pairs of these contracts.

import pytest  # noqa: E402

from app.mm.config import quote_symbol  # noqa: E402


@pytest.mark.parametrize(("faceunit", "unit", "expected"), [
    # Index futures: FACEUNIT names the settlement currency, the quote is points.
    ("USR", "В пунктах",                        "пт"),   # RTS, RVI
    ("RUB", "В пунктах",                        "пт"),   # MIX
    ("RUB", "в пунктах",                        "пт"),   # RGBI (lowercase in ISS)
    ("CNY", "В пунктах",                        "пт"),   # MOEXCNY
    # Everything else keeps its FACEUNIT currency.
    ("USR", "в долларах США за 1 тр.унцию",     "$"),    # GOLD
    ("USR", "в долларах США за 1 баррель",      "$"),    # WTI
    ("RUB", "в рублях за лот",                  "₽"),    # Si
    ("RUB", "в рублях за 1 тонну",              "₽"),    # WHEAT
    # No FACEUNIT → fall back to the free-text unit.
    (None,  "в долларах США за 1 баррель",      "$"),
    (None,  "В пунктах",                        "пт"),
    (None,  None,                               "₽"),
])
def test_quote_symbol(faceunit, unit, expected):
    assert quote_symbol(faceunit, unit) == expected
