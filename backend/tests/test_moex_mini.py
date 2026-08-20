"""Mini FORTS contracts (BRM/NGM/GOLDM/SILVM) are summed into the parent asset."""

from app.moex.config import (
    ASSET_ISS_CODE,
    ASSET_ISS_MINI_CODE,
    ASSET_TO_CANONICAL,
    iss_codes_for,
)


def test_mini_codes_keyed_by_known_assets():
    assert set(ASSET_ISS_MINI_CODE) <= set(ASSET_ISS_CODE)
    assert set(ASSET_ISS_MINI_CODE) <= set(ASSET_TO_CANONICAL)


def test_mini_codes_do_not_collide_with_main_codes():
    assert not set(ASSET_ISS_MINI_CODE.values()) & set(ASSET_ISS_CODE.values())


def test_iss_codes_for_returns_main_plus_mini():
    assert iss_codes_for("BR") == ("BR", "BRM")
    assert iss_codes_for("NG") == ("NG", "NGM")
    assert iss_codes_for("GD") == ("GOLD", "GOLDM")
    assert iss_codes_for("SV") == ("SILV", "SILVM")
    # assets without a mini keep the single main code
    assert iss_codes_for("BTC") == ("BTC",)
    assert iss_codes_for("NASD") == ("NASD",)
