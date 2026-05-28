import asyncio
import ccxt.pro as ccxtpro
from app.collector.base import BaseCollector
from app.config import settings


class BinanceCollector(BaseCollector):
    exchange_id = "binance"

    def _make_exchange(self, market_type: str = "spot") -> ccxtpro.binance:
        return ccxtpro.binance({
            "apiKey": settings.binance_api_key or None,
            "secret": settings.binance_secret or None,
            "enableRateLimit": True,
            "options": {"defaultType": market_type},
        })

    async def _connect(self):
        tasks = []
        if self.spot_symbols:
            tasks.append(self._run_market(self.spot_symbols, "spot"))
        if self.perp_symbols:
            # Binance USDT-M futures (XAU/USDT:USDT etc.)
            tasks.append(self._run_market(self.perp_symbols, "future"))
        if tasks:
            await asyncio.gather(*tasks)

    async def _run_market(self, symbols: list[str], market_type: str):
        exchange = self._make_exchange(market_type)
        try:
            await self._stream_tickers(exchange, symbols)
        finally:
            await exchange.close()
