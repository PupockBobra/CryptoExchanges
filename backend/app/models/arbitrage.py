from pydantic import BaseModel
from datetime import datetime


class ArbitrageAlert(BaseModel):
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    spread_pct: float
    ts: datetime
