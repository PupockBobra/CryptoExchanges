import json
from fastapi import APIRouter, HTTPException

from app.db.timescale import (
    fetch_instruments,
    create_instrument,
    update_instrument,
    delete_instrument,
)
from app.models.instrument import InstrumentCreate, InstrumentUpdate
from app.redis_client import get_redis

router = APIRouter()


def _row_to_dict(row) -> dict:
    d = dict(row)
    # asyncpg returns JSONB as a string; parse it back to dict
    if isinstance(d.get("aliases"), str):
        d["aliases"] = json.loads(d["aliases"])
    return d


@router.get("/")
async def list_instruments():
    rows = await fetch_instruments()
    return [_row_to_dict(r) for r in rows]


@router.post("/", status_code=201)
async def add_instrument(body: InstrumentCreate):
    row = await create_instrument(
        canonical=body.canonical,
        type_=body.type,
        base_asset=body.base_asset,
        quote_asset=body.quote_asset,
        description=body.description,
        enabled=body.enabled,
        aliases=body.aliases,
    )
    if row is None:
        raise HTTPException(409, "Instrument already exists")
    await _signal_reload()
    return _row_to_dict(row)


@router.patch("/{id}")
async def edit_instrument(id: int, body: InstrumentUpdate):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "No fields to update")
    row = await update_instrument(id, **fields)
    if row is None:
        raise HTTPException(404, "Instrument not found")
    await _signal_reload()
    return _row_to_dict(row)


@router.delete("/{id}", status_code=204)
async def remove_instrument(id: int):
    deleted = await delete_instrument(id)
    if not deleted:
        raise HTTPException(404, "Instrument not found")
    await _signal_reload()


@router.post("/reload", status_code=202)
async def trigger_reload():
    """Manually signal the collector worker to reload its symbol list."""
    await _signal_reload()
    return {"status": "reload signal sent"}


async def _signal_reload():
    r = await get_redis()
    await r.publish("instruments:reload", "1")
