"""
TTL cache used by the analytics endpoints.

The chart endpoints recompute multi-table aggregates on every request while the
data underneath only moves on an ETL pass, so responses are memoized for a
couple of minutes.  What matters: repeat calls skip the work, different
arguments do NOT share an entry, concurrent cold callers run the query once, and
the entry actually expires.
"""

import asyncio

import pytest

from app.api import cache
from app.api.cache import clear_cache, ttl_cache


@pytest.fixture(autouse=True)
def clean():
    clear_cache()
    cache._locks.clear()
    yield
    clear_cache()
    cache._locks.clear()


def test_repeat_calls_reuse_the_first_result():
    calls = []

    @ttl_cache(60)
    async def endpoint(days: int = 7):
        calls.append(days)
        return {"days": days}

    async def run():
        return [await endpoint(days=7), await endpoint(days=7)]

    first, second = asyncio.run(run())
    assert calls == [7]
    assert first == second == {"days": 7}


def test_different_arguments_are_cached_separately():
    calls = []

    @ttl_cache(60)
    async def endpoint(group: str):
        calls.append(group)
        return group

    async def run():
        await endpoint("shares")
        await endpoint("currency")
        await endpoint("shares")

    asyncio.run(run())
    assert calls == ["shares", "currency"]


def test_concurrent_cold_callers_run_the_query_once():
    """A page opens several charts at once — a cold cache must not fan the same
    expensive query out to every one of them."""
    calls = []

    @ttl_cache(60)
    async def endpoint():
        calls.append(1)
        await asyncio.sleep(0.01)     # the slow query
        return "rows"

    async def run():
        return await asyncio.gather(*[endpoint() for _ in range(5)])

    assert asyncio.run(run()) == ["rows"] * 5
    assert calls == [1]


def test_entry_expires_after_the_ttl():
    calls = []

    @ttl_cache(0.01)
    async def endpoint():
        calls.append(1)
        return len(calls)

    async def run():
        await endpoint()
        await asyncio.sleep(0.02)
        return await endpoint()

    assert asyncio.run(run()) == 2
    assert calls == [1, 1]


def test_clear_cache_drops_entries():
    calls = []

    @ttl_cache(60)
    async def endpoint():
        calls.append(1)
        return len(calls)

    async def run():
        await endpoint()
        clear_cache()
        return await endpoint()

    assert asyncio.run(run()) == 2


def test_signature_survives_so_fastapi_still_sees_query_params():
    """FastAPI builds query parameters from the handler's signature; the
    decorator must not hide it behind (*args, **kwargs)."""
    import inspect

    @ttl_cache(60)
    async def endpoint(group: str, days: int = 7):
        return group, days

    params = inspect.signature(endpoint).parameters
    assert list(params) == ["group", "days"]
    assert params["days"].default == 7
