"""
Read-through TTL cache for cost data.

In the MCP model, an agentic session asks the same cost question several times
while cross-referencing. Without caching, every call re-hits the cloud billing
API, and AWS Cost Explorer bills $0.01 per request, so one conversation can
quietly run up real API cost. This cache makes the first call fetch and every
repeat within the TTL free.

It is passive by design. Entries are populated on read and served until they
expire. There is no timer, no polling, no background refresh. It can only ever
call the underlying API when a caller asks and the stored copy is missing or
stale, so it cannot "call the API every few seconds" on its own.

TTLs are tuned to how fast the source actually changes:
  - AWS Cost Explorer / CUR refresh about 3x/day at daily granularity, so 12h
    is safe and costs nothing in accuracy
  - Kubernetes cluster state moves faster, so a few minutes
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Awaitable, Callable

# TTLs in seconds, env-overridable.
COST_TTL = int(os.getenv("FINOPS_COST_CACHE_TTL", str(12 * 3600)))  # 12 hours
K8S_TTL = int(os.getenv("FINOPS_K8S_CACHE_TTL", "300"))             # 5 minutes
DEFAULT_TTL = int(os.getenv("FINOPS_CACHE_TTL", "3600"))            # 1 hour

# Bound the store so a long-lived server cannot grow without limit.
_MAX_ENTRIES = int(os.getenv("FINOPS_CACHE_MAX_ENTRIES", "512"))

# Global kill switch to force fresh data (e.g. for debugging or a --no-cache run).
_DISABLED = os.getenv("FINOPS_CACHE_DISABLED", "").lower() in ("1", "true", "yes")

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, value)


def make_key(*parts: Any) -> str:
    """Build a stable cache key from arbitrary parts."""
    return "|".join(str(p) for p in parts)


def get(key: str) -> Any | None:
    """Return the cached value if present and unexpired, else None."""
    if _DISABLED:
        return None
    now = time.time()
    with _lock:
        item = _store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if now >= expires_at:
            _store.pop(key, None)
            return None
        return value


def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    """Store a value under key for ttl seconds."""
    if _DISABLED:
        return
    now = time.time()
    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            # Drop expired entries first; if still full, drop the soonest-to-expire.
            for k in [k for k, (exp, _) in _store.items() if now >= exp]:
                _store.pop(k, None)
            if len(_store) >= _MAX_ENTRIES:
                _store.pop(min(_store, key=lambda k: _store[k][0]), None)
        _store[key] = (now + ttl, value)


def clear() -> None:
    """Drop everything. Used after a fresh snapshot or on explicit refresh."""
    with _lock:
        _store.clear()


async def aget_or_set(key: str, ttl: int, producer: Callable[[], Awaitable[Any]]) -> Any:
    """Async read-through: return the cached value, or run producer() once and
    cache it. On a rare concurrent miss two producers may run, which costs one
    extra API call and is not worth locking the event loop to prevent.
    """
    hit = get(key)
    if hit is not None:
        return hit
    value = await producer()
    set(key, value, ttl)
    return value
