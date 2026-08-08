"""Shared-state store backed by Redis, with an in-memory fallback for dev.

Used by:
  * JWT denylist (token revocation)
  * distributed rate-limit counters
  * callback replay-nonce store

In production (REDIS_URL set) this is a real Redis. In DEV_MODE without
REDIS_URL it falls back to process-local dicts — fine for a single local
instance but state is NOT shared and is lost on restart. The prod config
guard (config.enforce_prod_config) refuses to boot without REDIS_URL in
non-dev mode, so the fallback never runs in production.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from config import REDIS_URL, is_dev_mode

_redis = None
_redis_lock = asyncio.Lock()

# ---- in-memory fallback (dev only) -----------------------------------------
_mem_store: dict[str, tuple[str, float]] = {}  # key -> (value, expire_at|0)
_mem_counters: dict[str, list[float]] = {}     # key -> [timestamps]


async def _get_redis():
    """Lazily connect to Redis and return a client, or None in dev fallback."""
    global _redis
    if not REDIS_URL:
        return None
    if _redis is None:
        async with _redis_lock:
            if _redis is None:
                import redis.asyncio as aioredis  # type: ignore
                _redis = aioredis.from_url(
                    REDIS_URL, decode_responses=True, socket_timeout=2.0
                )
    return _redis


# ── generic helpers ──────────────────────────────────────────────────────────

async def setex(key: str, value: str, ttl_seconds: int) -> None:
    """Set key→value with a TTL (seconds)."""
    r = await _get_redis()
    if r is not None:
        await r.set(key, value, ex=ttl_seconds)
    else:
        _mem_store[key] = (value, time.time() + ttl_seconds)


async def get(key: str) -> Optional[str]:
    r = await _get_redis()
    if r is not None:
        return await r.get(key)
    tup = _mem_store.get(key)
    if tup is None:
        return None
    value, expire_at = tup
    if expire_at and expire_at < time.time():
        _mem_store.pop(key, None)
        return None
    return value


async def delete(key: str) -> None:
    r = await _get_redis()
    if r is not None:
        await r.delete(key)
    else:
        _mem_store.pop(key, None)


async def exists(key: str) -> bool:
    return (await get(key)) is not None


# ── atomic primitives ────────────────────────────────────────────────────────

async def set_if_absent(key: str, value: str, ttl_seconds: int) -> bool:
    """Atomic SETNX-with-TTL. Returns True if the key was set (i.e. it did not
    already exist), False if it already existed.

    Used for the callback replay-nonce store: the first caller wins.
    """
    r = await _get_redis()
    if r is not None:
        # SET key value NX EX ttl  → returns OK if set, None if already exists
        res = await r.set(key, value, ex=ttl_seconds, nx=True)
        return bool(res)
    # in-memory approximation (dev only — not truly atomic under concurrency,
    # but fine for single-process local dev)
    if await exists(key):
        return False
    await setex(key, value, ttl_seconds)
    return True


async def fixed_window_count(key: str, window_seconds: int, limit: int) -> tuple[int, bool]:
    """Increment a fixed-window counter and return (count, allowed).

    Allowed is True when count <= limit. The counter is created with a TTL of
    window_seconds on first increment.
    """
    r = await _get_redis()
    if r is not None:
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count), count <= limit
    # in-memory fallback
    now = time.time()
    ts = _mem_counters.get(key, [])
    ts = [t for t in ts if now - t < window_seconds]
    ts.append(now)
    _mem_counters[key] = ts
    return len(ts), len(ts) <= limit


async def health() -> bool:
    """Return True if the store is usable (Redis ping or dev fallback)."""
    r = await _get_redis()
    if r is None:
        return True  # dev fallback always "healthy"
    try:
        return bool(await r.ping())
    except Exception:
        return False
