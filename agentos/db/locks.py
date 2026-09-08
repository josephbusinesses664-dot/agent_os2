"""Distributed locking (master spec §34: queues / concurrency).

Many agents may operate simultaneously — across processes and machines in
production (the box runs app + worker containers against one Redis). Without
locks, two workers can race the same task: double execution, double spending,
duplicate deployments, inconsistent task state. The DB-status guard in the
worker loop is a first line, but it is racy across processes; the lock is the
arbiter.

Design:
    acquire   → `SET key owner NX PX ttl_ms` (atomic: only one owner wins)
    release   → Lua compare-and-delete (only the owner can release)
    is_locked → EXISTS (with TTL expiry)

When no Redis is configured (offline / tests) a process-local asyncio mutex
map provides the same API with the same semantics, so concurrency bugs stay
visible in CI without a live Redis.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger("agentos.locks")

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class LockManager:
    """Distributed mutexes with a Redis backend and an in-memory fallback.

    Usage:
        ok = await locks.acquire("task:abc:exec", ttl=120)
        try:
            ... critical section ...
        finally:
            await locks.release("task:abc:exec", owner)
    """

    def __init__(self, redis_url: Optional[str] = None,
                 default_ttl: float = 120.0) -> None:
        self.redis_url = redis_url
        self.default_ttl = default_ttl
        self._redis = None
        if redis_url:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(redis_url, decode_responses=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis unavailable (%s) — using in-process locks", exc)
                self._redis = None
        self._local: dict[str, tuple[str, float]] = {}  # key -> (owner, expires_at)
        self._local_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def acquire(self, key: str, ttl: Optional[float] = None,
                      owner: Optional[str] = None) -> bool:
        """Try to lock `key`. Returns True only if this caller won."""
        owner = owner or uuid.uuid4().hex
        ttl = ttl if ttl is not None else self.default_ttl
        if self._redis is not None:
            ok = await self._redis.set(key, owner, nx=True, px=int(ttl * 1000))
            return bool(ok)
        return await self._acquire_local(key, owner, ttl)

    async def release(self, key: str, owner: str) -> bool:
        """Release `key`, but only if `owner` still holds it."""
        if self._redis is not None:
            # EVAL directly (works on real Redis and fakeredis alike; the
            # compare-and-delete makes release safe under races)
            return bool(await self._redis.eval(_RELEASE_SCRIPT, 1, key, owner))
        return await self._release_local(key, owner)

    async def is_locked(self, key: str) -> bool:
        if self._redis is not None:
            return bool(await self._redis.exists(key))
        async with self._local_guard:
            entry = self._local.get(key)
            if entry is None:
                return False
            owner, expires_at = entry
            if time.monotonic() >= expires_at:
                self._local.pop(key, None)
                return False
            return True

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # In-process fallback (single process: still enforces mutual exclusion
    # between concurrent asyncio tasks, so races surface in tests).
    # ------------------------------------------------------------------
    async def _acquire_local(self, key: str, owner: str, ttl: float) -> bool:
        async with self._local_guard:
            entry = self._local.get(key)
            if entry is not None:
                _owner, expires_at = entry
                if time.monotonic() < expires_at:
                    return False  # held and not expired
                self._local.pop(key, None)
            self._local[key] = (owner, time.monotonic() + ttl)
            return True

    async def _release_local(self, key: str, owner: str) -> bool:
        async with self._local_guard:
            entry = self._local.get(key)
            if entry is None:
                return False
            held_by, _expires_at = entry
            if held_by != owner:
                return False  # only the owner may release
            self._local.pop(key, None)
            return True