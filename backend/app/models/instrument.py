from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class InstrumentCreate(BaseModel):
    canonical:   str
    type:        str = "spot"       # "spot" | "perp"
    base_asset:  str = ""
    quote_asset: str = "USDT"
    description: str = ""
    enabled:     bool = True
    aliases:     dict = {}          # {exchange_id: symbol_override | null}


class InstrumentUpdate(BaseModel):
    canonical:   Optional[str]  = None
    type:        Optional[str]  = None
    base_asset:  Optional[str]  = None
    quote_asset: Optional[str]  = None
    description: Optional[str]  = None
    enabled:     Optional[bool] = None
    aliases:     Optional[dict] = None


class Instrument(BaseModel):
    id:          int
    canonical:   str
    type:        str
    base_asset:  str
    quote_asset: str
    description: str
    enabled:     bool
    aliases:     dict
    created_at:  datetime
    updated_at:  datetime
