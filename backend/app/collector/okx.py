import ccxt.pro as ccxtpro
from app.collector.base import BaseCollector
from app.config import settings


class OkxCollector(BaseCollector):
    exchange_id = "okx"

    def _make_exchange(self) -> ccxtpro.okx:
        return ccxtpro.okx({
            "apiKey": settings.okx_api_key or None,
            "secret": settings.okx_secret or None,
            "password": settings.okx_passphrase or None,
            "enableRateLimit": True,
        })

    async def _connect(self):
        # OKX resolves spot vs swap automatically from the unified symbol
        # (XAU/USDT:USDT routes to the SWAP market, BTC/USDT routes to spot)
        exchange = self._make_exchange()
        try:
            await self._stream_tickers(exchange, self.symbols)
        finally:
            await exchange.close()
