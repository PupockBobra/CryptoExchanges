"""
Short-lived in-process cache for the analytics endpoints.

Every chart endpoint recomputes a multi-table aggregate on each request (0.2–6 s
of database work), while the data underneath only moves when an ETL pass lands —
every 6 h for turnover/OI, every 15 min for the spread buckets.  Two people on
the same page, or one person switching tabs back and forth, paid the full query
each time.

A short TTL absorbs that without going stale in any way a user would notice.
Live endpoints (order books, spread-live, prices) are deliberately NOT cached.

Not shared between workers — and it must not be: uvicorn runs a single worker
(see the `--workers` gotcha in CLAUDE.md).
"""

import asyncio
import functools
import time
from typing import Any, Callable

DEFAULT_TTL = 120.0  # seconds

_store: dict[Any, tuple[float, Any]] = {}
_locks: dict[Any, asyncio.Lock] = {}


def ttl_cache(seconds: float = DEFAULT_TTL) -> Callable:
    """
    Memoize an async endpoint by its arguments for ``seconds``.

    The per-key lock keeps a cold cache from letting N concurrent requests all
    run the same expensive query (a page opens several charts at once).
    ``functools.wraps`` keeps the original signature visible, which is what
    FastAPI reads to build query parameters — so the decorator is invisible to
    the route declaration.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = (fn.__module__, fn.__qualname__, args, tuple(sorted(kwargs.items())))
            hit = _store.get(key)
            if hit is not None and time.monotonic() - hit[0] < seconds:
                return hit[1]

            lock = _locks.setdefault(key, asyncio.Lock())
            async with lock:
                hit = _store.get(key)          # another waiter may have filled it
                if hit is not None and time.monotonic() - hit[0] < seconds:
                    return hit[1]
                value = await fn(*args, **kwargs)
                _store[key] = (time.monotonic(), value)
                return value
        return wrapper
    return decorator


def clear_cache() -> None:
    """Drop every cached response — called after a manual ETL refresh."""
    _store.clear()
