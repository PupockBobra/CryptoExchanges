import json
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://arbi:arbi@localhost:5432/arbidb"
    redis_url: str = "redis://localhost:6379/0"

    arbi_threshold_pct: float = 0.3
    # Spot pairs and USDT-margined perps (symbol:USDT suffix = perpetual future)
    arbi_symbols: str = "BTC/USDT,ETH/USDT,SOL/USDT,XAU/USDT:USDT,XAG/USDT:USDT"

    # Optional per-exchange symbol overrides (JSON).
    # Maps canonical ccxt symbol → {exchange_id: exchange_symbol}.
    # Use when an exchange lists a pair under a different unified name.
    # Example: '{"XAU/USDT:USDT": {"bybit": "XAU/USDT:USDT", "okx": "XAU/USDT:USDT"}}'
    symbol_aliases: str = "{}"

    binance_api_key: str = ""
    binance_secret: str = ""
    okx_api_key: str = ""
    okx_secret: str = ""
    okx_passphrase: str = ""
    bybit_api_key: str = ""
    bybit_secret: str = ""
    mexc_api_key: str = ""
    mexc_secret: str = ""

    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173"

    @property
    def symbols(self) -> List[str]:
        return [s.strip() for s in self.arbi_symbols.split(",") if s.strip()]

    @property
    def spot_symbols(self) -> List[str]:
        return [s for s in self.symbols if ":" not in s]

    @property
    def perp_symbols(self) -> List[str]:
        return [s for s in self.symbols if ":" in s]

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def symbol_aliases_dict(self) -> dict:
        """Parsed SYMBOL_ALIASES: {canonical_sym: {exchange_id: exchange_sym}}"""
        try:
            return json.loads(self.symbol_aliases)
        except Exception:
            return {}

    exchanges: List[str] = ["binance", "okx", "bybit", "mexc", "hyperliquid"]


settings = Settings()
