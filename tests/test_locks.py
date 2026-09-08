"""Distributed locking (master spec §34: queues / concurrency).

Tests cover the in-process fallback (used offline/in CI), the Redis backend
(via fakeredis, same semantics), and the worker-loop integration: two workers
can never execute the same task simultaneously.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# In-process fallback (no Redis)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutual_exclusion_in_process():
    from agentos.db.locks import LockManager

    lm = LockManager()
    assert await lm.acquire("task:1:exec", owner="worker-a")
    # second owner cannot get the same lock while held
    assert not await lm.acquire("task:1:exec", owner="worker-b")
    assert await lm.is_locked("task:1:exec")
    # only the owner can release
    assert not await lm.release("task:1:exec", "worker-b")
    assert await lm.release("task:1:exec", "worker-a")
    assert not await lm.is_locked("task:1:exec")
    # re-acquirable after release
    assert await lm.acquire("task:1:exec", owner="worker-b")
    await lm.release("task:1:exec", "worker-b")


@pytest.mark.asyncio
async def test_ttl_expiry_releases_lock():
    from agentos.db.locks import LockManager

    lm = LockManager(default_ttl=0.05)
    assert await lm.acquire("k")
    await asyncio_sleep(0.1)
    assert not await lm.is_locked("k")
    # expired locks are re-acquirable
    assert await lm.acquire("k", owner="other")


@pytest.mark.asyncio
async def test_concurrent_tasks_mutual_exclusion():
    """Two coroutines racing the same key: exactly one wins."""
    from agentos.db.locks import LockManager

    lm = LockManager()
    winners = []

    async def racer(owner: str) -> None:
        if await lm.acquire("shared", owner=owner):
            winners.append(owner)
            await asyncio_sleep(0.02)  # hold it briefly
            await lm.release("shared", owner)

    import asyncio
    await asyncio.gather(racer("a"), racer("b"), racer("c"))
    assert len(winners) == 1


# ---------------------------------------------------------------------------
# Redis backend (fakeredis — same command semantics as a real server)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_lock_cross_client_exclusion():
    from agentos.db.locks import LockManager
    import fakeredis.aioredis

    backend = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker1 = LockManager()
    worker1._redis = backend
    worker2 = LockManager()
    worker2._redis = backend

    # worker1 holds -> worker2 (separate client, separate process) is excluded
    assert await worker1.acquire("task:9:exec", owner="w1")
    assert not await worker2.acquire("task:9:exec", owner="w2")
    # wrong owner cannot release
    assert not await worker2.release("task:9:exec", "w2")
    # owner releases -> next worker acquires
    assert await worker1.release("task:9:exec", "w1")
    assert await worker2.acquire("task:9:exec", owner="w2")
    # released by the right owner only
    assert not await worker1.release("task:9:exec", "w1")
    assert await worker2.release("task:9:exec", "w2")


@pytest.mark.asyncio
async def test_redis_lock_ttl():
    from agentos.db.locks import LockManager
    import fakeredis.aioredis

    backend = fakeredis.aioredis.FakeRedis(decode_responses=True)
    lm = LockManager()
    lm._redis = backend
    assert await lm.acquire("k", owner="x", ttl=0.05)
    await asyncio_sleep(0.1)
    assert not await lm.is_locked("k")
    assert await lm.acquire("k", owner="y")


# ---------------------------------------------------------------------------
# Worker-loop integration: the engine must never run a task twice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_loop_locks_task_execution(svc):
    """Two worker loops pulling the same queued task: only one executes it."""
    from agentos.db.locks import LockManager
    import asyncio

    # in-memory queue seeded with the same task twice (a duplicated enqueue
    # that can happen under races); one worker wins the lock, the other skips.
    task = await svc.tasks.create(
        title="locked task", project_id="p_test", assigned_agent="ai-engineer")
    await svc.queue.enqueue({"task_id": task.task_id})
    await svc.queue.enqueue({"task_id": task.task_id})

    executed = []
    original = svc.engine.run_single_task

    async def counting_run(task_id: str):
        executed.append(task_id)
        return await original(task_id)

    svc.engine.run_single_task = counting_run
    # point both workers at the same LockManager (shared backend)
    lock_manager = LockManager()
    svc.locks = lock_manager

    stop = asyncio.Event()

    async def run_worker() -> None:
        await svc.engine.worker_loop(stop)

    w1 = asyncio.create_task(run_worker())
    w2 = asyncio.create_task(run_worker())
    # let both workers spin a moment, then stop them
    await asyncio.sleep(0.4)
    stop.set()
    await asyncio.gather(w1, w2)

    assert len(executed) == 1, f"task executed {len(executed)} times: {executed}"
    # the losing worker skipped because the lock was held — no duplicate run
    assert executed == [task.task_id]


# ---------------------------------------------------------------------------

async def asyncio_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)