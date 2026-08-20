"""exchanges.py invariants: the ccxt keysort patch and the perp-override map."""

from ccxt.base.exchange import Exchange as CcxtExchange

from app.config import settings
from app.exchanges import CRYPTO_PERP_OVERRIDES, EXCHANGE_CLS, PERP_MARKET_TYPE


def test_keysort_patch_survives_none_keys():
    # OKX intermittently returns a market with id=None; unpatched keysort
    # raised TypeError and killed the whole load_markets(). None must sort last.
    result = CcxtExchange.keysort({None: 1, "b": 2, "a": 3})
    assert list(result.keys()) == ["a", "b", None]


def test_every_configured_exchange_has_class_and_perp_type():
    for ex_id in settings.exchanges:
        assert ex_id in EXCHANGE_CLS, f"missing ccxt class for {ex_id}"
        assert ex_id in PERP_MARKET_TYPE, f"missing perp market type for {ex_id}"


def test_crypto_perp_overrides_cover_all_exchanges():
    # BTC/ETH/SOL volume+OI come from perps on every venue; hyperliquid is
    # USDC-margined, the rest USDT.
    for canonical, per_ex in CRYPTO_PERP_OVERRIDES.items():
        assert ":" not in canonical            # canonical stays the spot symbol
        assert set(per_ex) == set(settings.exchanges)
        base = canonical.split("/")[0]
        assert per_ex["hyperliquid"] == f"{base}/USDC:USDC"
        for ex_id in ("binance", "okx", "bybit", "mexc"):
            assert per_ex[ex_id] == f"{base}/USDT:USDT"
