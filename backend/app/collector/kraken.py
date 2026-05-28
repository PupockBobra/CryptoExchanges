import ccxt.pro as ccxtpro
from app.collector.base import BaseCollector
from app.config import settings


class KrakenCollector(BaseCollector):
    exchange_id = "kraken"

    async def _connect(self):
        exchange = ccxtpro.kraken({
            "apiKey": settings.kraken_api_key or None,
            "secret": settings.kraken_secret or None,
            "enableRateLimit": True,
        })
        try:
            while True:
                for symbol in self.symbols:
                    ticker = await exchange.watch_ticker(symbol)
                    bid = ticker.get("bid") or ticker.get("last", 0)
                    ask = ticker.get("ask") or ticker.get("last", 0)
                    last = ticker.get("last", 0)
                    if last:
                        await self._publish(symbol, bid, ask, last)
        finally:
            await exchange.close()
