"""Collector symbol-alias resolution (collector/base.py).

Resolution order: DB alias (null = skip exchange) → env SYMBOL_ALIASES →
canonical passthrough. A wrong resolution silently misroutes every tick.
"""

from app.collector.base import BaseCollector, redis_channel
from app.config import settings


class _Dummy(BaseCollector):
    exchange_id = "binance"

    async def _connect(self):  # pragma: no cover - never streamed in tests
        pass


def _make(db_aliases=None):
    return _Dummy(symbols=["BTC/USDT"], db_aliases=db_aliases)


def test_redis_channel_format():
    assert redis_channel("BTC/USDT") == "prices:BTC_USDT"
    assert redis_channel("XAU/USDT:USDT") == "prices:XAU_USDT_USDT"


def test_passthrough_when_no_alias():
    c = _make()
    assert c._resolve_symbols(["BTC/USDT"]) == {"BTC/USDT": "BTC/USDT"}


def test_db_alias_wins():
    c = _make(db_aliases={"WTI/USDT:USDT": {"binance": "CL/USDT:USDT"}})
    assert c._resolve_symbols(["WTI/USDT:USDT"]) == {"CL/USDT:USDT": "WTI/USDT:USDT"}


def test_explicit_null_alias_skips_symbol_on_this_exchange():
    c = _make(db_aliases={"WTI/USDT:USDT": {"binance": None}})
    assert c._resolve_symbols(["WTI/USDT:USDT"]) == {}


def test_env_alias_used_when_no_db_alias(monkeypatch):
    monkeypatch.setattr(
        settings, "symbol_aliases",
        '{"XAU/USDT:USDT": {"binance": "XAUT/USDT:USDT"}}',
    )
    c = _make()
    assert c._resolve_symbols(["XAU/USDT:USDT"]) == {"XAUT/USDT:USDT": "XAU/USDT:USDT"}


def test_db_alias_overrides_env_alias(monkeypatch):
    monkeypatch.setattr(
        settings, "symbol_aliases",
        '{"XAU/USDT:USDT": {"binance": "FROM_ENV"}}',
    )
    c = _make(db_aliases={"XAU/USDT:USDT": {"binance": "FROM_DB"}})
    assert c._resolve_symbols(["XAU/USDT:USDT"]) == {"FROM_DB": "XAU/USDT:USDT"}


def test_alias_collision_keeps_first_symbol():
    # Two canonicals resolving to one exchange symbol can only route to one —
    # the first wins, the second is dropped (with a warning).
    c = _make(db_aliases={
        "BRN/USDT:USDT": {"binance": "OIL/USDT:USDT"},
        "WTI/USDT:USDT": {"binance": "OIL/USDT:USDT"},
    })
    resolved = c._resolve_symbols(["BRN/USDT:USDT", "WTI/USDT:USDT"])
    assert resolved == {"OIL/USDT:USDT": "BRN/USDT:USDT"}
