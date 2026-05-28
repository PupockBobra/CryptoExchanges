import asyncio
import ccxt.pro as ccxtpro
from app.collector.base import BaseCollector
from app.config import settings


class BybitCollector(BaseCollector):
    exchange_id = "bybit"

    def _make_exchange(self, market_type: str = "spot") -> ccxtpro.bybit:
        return ccxtpro.bybit({
            "apiKey": settings.bybit_api_key or None,
            "secret": settings.bybit_secret or None,
            "enableRateLimit": True,
            "options": {"defaultType": market_type},
        })

    async def _connect(self):
        tasks = []
        if self.spot_symbols:
            tasks.append(self._run_market(self.spot_symbols, "spot"))
        if self.perp_symbols:
            # Bybit linear = USDT-margined perpetuals (XAUUSDT, XAGUSDT, etc.)
            tasks.append(self._run_market(self.perp_symbols, "linear"))
        if tasks:
            await asyncio.gather(*tasks)

    async def _run_market(self, symbols: list[str], market_type: str):
        exchange = self._make_exchange(market_type)
        try:
            await self._stream_tickers(exchange, symbols)
        finally:
            await exchange.close()
