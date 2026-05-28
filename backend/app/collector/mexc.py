import asyncio
import ccxt.pro as ccxtpro
from app.collector.base import BaseCollector
from app.config import settings


class MexcCollector(BaseCollector):
    exchange_id = "mexc"

    def _make_exchange(self, market_type: str = "spot") -> ccxtpro.mexc:
        return ccxtpro.mexc({
            "apiKey": settings.mexc_api_key or None,
            "secret": settings.mexc_secret or None,
            "enableRateLimit": True,
            "options": {"defaultType": market_type},
        })

    async def _connect(self):
        tasks = []
        if self.spot_symbols:
            tasks.append(self._run_market(self.spot_symbols, "spot"))
        if self.perp_symbols:
            # MEXC swap = USDT-margined perpetuals
            tasks.append(self._run_market(self.perp_symbols, "swap"))
        if tasks:
            await asyncio.gather(*tasks)

    async def _run_market(self, symbols: list[str], market_type: str):
        exchange = self._make_exchange(market_type)
        try:
            await self._stream_tickers(exchange, symbols)
        finally:
            await exchange.close()
