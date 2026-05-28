from pydantic import BaseModel
from datetime import datetime


class PriceTick(BaseModel):
    exchange: str
    symbol: str
    bid: float
    ask: float
    last: float
    ts: datetime


class PriceHistory(BaseModel):
    exchange: str
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    bucket: datetime
