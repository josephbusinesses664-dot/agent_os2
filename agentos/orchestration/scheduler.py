"""Scheduler — recurring / standing missions (master spec Phase 3 scheduling).

A `Schedule` describes a mission that fires when its `next_run_at` arrives:
the scheduler claims the occurrence (advances the state) under a distributed
lock so that only one worker launches each fire, then runs the goal through
the engine's durable workflow machinery (or through any injected `fire`
callback).

Design rules:
* Claim-before-launch: the schedule's `next_run_at` / `run_count` advance
  *before* the mission is launched. A crash between claim and launch can
  therefore never double-fire the same occurrence — the occurrence is already
  marked used. (The mission itself is durable: the engine writes checkpoints,
  so a crash *during* a run resumes from the last checkpoint.)
* Distributed mutual exclusion: `schedule:<id>:fire` locks (Redis) prevent two
  workers/processes from claiming the same fire.
* Deadline enforcement: tasks carrying a `deadline` that pass it while still
  pending/queued are escalated (blocked + message to the responsible agent's
  manager + event), rather than silently rotting in the queue.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from agentos.db.store import EntityStore
from agentos.domain.models import Schedule, TaskStatus, new_id, utcnow

logger = logging.getLogger("agentos.scheduler")

# Fire callback signature: async (schedule) -> dict (e.g. {"run_id": ...})
FireCallback = Callable[[Schedule], Awaitable[dict]]


class SchedulerService:
    def __init__(self, store: EntityStore, *, event_bus: Optional[Any] = None,
                 locks: Optional[Any] = None) -> None:
        self.store = store
        self._collection = "schedules"
        self._events = event_bus
        self.locks = locks
        self._owner = f"scheduler:{id(self)}"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def create(self, name: str, goal: str, *,
                     interval_seconds: int = 86400,
                     workflow_id: Optional[str] = None,
                     project_id: Optional[str] = None,
                     user_id: str = "human", max_runs: Optional[int] = None,
                     start_at: Optional[Any] = None,
                     created_by: str = "human") -> Schedule:
        schedule = Schedule(
            schedule_id=new_id("sched"),
            name=name, goal=goal,
            interval_seconds=max(1, int(interval_seconds)),
            workflow_id=workflow_id, project_id=project_id,
            user_id=user_id, max_runs=max_runs,
            next_run_at=start_at or utcnow(), created_by=created_by,
        )
        await self.store.save(self._collection, schedule)
        if self._events:
            await self._events.publish("schedule.created", {
                "schedule_id": schedule.schedule_id, "name": name,
                "interval_seconds": schedule.interval_seconds,
                "workflow_id": workflow_id, "max_runs": max_runs,
            }, source="scheduler")
        return schedule

    async def get(self, schedule_id: str) -> Optional[Schedule]:
        return await self.store.get(self._collection, schedule_id, Schedule)

    async def require(self, schedule_id: str) -> Schedule:
        schedule = await self.get(schedule_id)
        if schedule is None:
            raise KeyError(f"schedule {schedule_id} not found")
        return schedule

    async def save(self, schedule: Schedule) -> Schedule:
        schedule.touch()
        await self.store.save(self._collection, schedule)
        return schedule

    async def list(self, enabled_only: bool = False) -> list[Schedule]:
        schedules = await self.store.list(self._collection, Schedule)
        schedules.sort(key=lambda s: s.next_run_at)
        if enabled_only:
            schedules = [s for s in schedules if s.enabled]
        return schedules

    async def delete(self, schedule_id: str) -> bool:
        schedule = await self.get(schedule_id)
        if schedule is None:
            return False
        await self.store.delete(self._collection, schedule_id)
        if self._events:
            await self._events.publish("schedule.deleted",
                                       {"schedule_id": schedule_id},
                                       source="scheduler")
        return True

    async def set_enabled(self, schedule_id: str, enabled: bool) -> Optional[Schedule]:
        schedule = await self.get(schedule_id)
        if schedule is None:
            return None
        schedule.enabled = enabled
        await self.save(schedule)
        if self._events:
            await self._events.publish(
                "schedule.enabled" if enabled else "schedule.paused",
                {"schedule_id": schedule_id}, source="scheduler")
        return schedule

    # ------------------------------------------------------------------
    # Due claim (the core scheduling primitive)
    # ------------------------------------------------------------------
    async def due(self, now: Optional[Any] = None) -> list[Schedule]:
        now = now or utcnow()
        out: list[Schedule] = []
        for schedule in await self.list():
            if not schedule.enabled:
                continue
            if schedule.next_run_at > now:
                continue
            if schedule.max_runs is not None and schedule.run_count >= schedule.max_runs:
                continue
            out.append(schedule)
        return out

    async def claim_due(self, now: Optional[Any] = None,
                        owner: Optional[str] = None) -> list[Schedule]:
        """Claim every due fire atomically. Returns the claimed schedules.

        Claiming advances `next_run_at` (now + interval) and increments
        `run_count` under `schedule:<id>:fire` so exactly one worker/process
        claims each occurrence. Schedules whose max_runs is reached are
        disabled as they are claimed.
        """
        now = now or utcnow()
        owner = owner or self._owner
        claimed: list[Schedule] = []
        for schedule in await self.due(now):
            lock_key = f"schedule:{schedule.schedule_id}:fire"
            if self.locks is not None and not await self.locks.acquire(lock_key, owner=owner):
                continue
            try:
                # re-read under the lock: another worker may have claimed it
                fresh = await self.get(schedule.schedule_id)
                if fresh is None or not fresh.enabled:
                    continue
                if fresh.next_run_at > now:
                    continue
                if fresh.max_runs is not None and fresh.run_count >= fresh.max_runs:
                    continue
                fresh.run_count += 1
                fresh.last_run_at = now
                fresh.last_status = "running"
                if fresh.max_runs is not None and fresh.run_count >= fresh.max_runs:
                    fresh.enabled = False
                fresh.next_run_at = now + timedelta(seconds=fresh.interval_seconds)
                await self.save(fresh)
                claimed.append(fresh)
            finally:
                if self.locks is not None:
                    await self.locks.release(lock_key, owner)
        return claimed

    # ------------------------------------------------------------------
    # Firing + tick
    # ------------------------------------------------------------------
    async def fire(self, schedule: Schedule,
                   fire_cb: FireCallback) -> dict:
        """Launch one claimed schedule via the callback, recording outcome.

        Never raises: a failed launch is recorded on the schedule so the next
        interval still runs (retry-with-backoff semantics at the schedule
        level — the mission never dies silently).
        """
        try:
            result = await fire_cb(schedule)
            schedule.last_status = "ok"
            schedule.last_error = None
            run_id = result.get("run_id") if isinstance(result, dict) else None
            if run_id:
                schedule.last_run_id = run_id
            await self.save(schedule)
            if self._events:
                await self._events.publish(
                    "schedule.fired",
                    {"schedule_id": schedule.schedule_id, "run_id": run_id,
                     "run_count": schedule.run_count},
                    source="scheduler")
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedule %s fire failed", schedule.schedule_id)
            schedule.last_status = "failed"
            schedule.last_error = str(exc)
            schedule.next_run_at = utcnow() + timedelta(
                seconds=schedule.interval_seconds)
            await self.save(schedule)
            if self._events:
                await self._events.publish(
                    "schedule.failed",
                    {"schedule_id": schedule.schedule_id, "error": str(exc)},
                    severity="error", source="scheduler")
            return {"status": "failed", "error": str(exc)}

    async def tick(self, fire_cb: Optional[FireCallback] = None,
                   now: Optional[Any] = None,
                   owner: Optional[str] = None) -> list[str]:
        """Claim due schedules and fire them. Returns launched schedule ids.

        Called periodically by the worker loop. Fire failures are captured by
        `fire()` (never raise out of tick); a launch *after* claim runs
        concurrently-safe because each occurrence is claimed exactly once.
        """
        if fire_cb is None:
            return []
        launched: list[str] = []
        for schedule in await self.claim_due(now=now, owner=owner):
            await self.fire(schedule, fire_cb)
            launched.append(schedule.schedule_id)
        return launched

    # ------------------------------------------------------------------
    # Deadline enforcement
    # ------------------------------------------------------------------
    async def enforce_deadlines(self, task_service: Any,
                                agent_registry: Any = None,
                                messages: Any = None,
                                now: Optional[Any] = None) -> list[str]:
        """Escalate tasks whose deadline has passed and are still unfinished.

        Only pending/queued tasks are escalated (a running task is already
        being worked). The task is BLOCKED with the deadline error, an event
        is published, and the responsible agent's manager (chain of command)
        receives a blocker message so a human/lead can reassign or reprioritize.
        """
        now = now or utcnow()
        escalated: list[str] = []
        for status in (TaskStatus.PENDING, TaskStatus.QUEUED):
            for task in await task_service.list(status=status, limit=1000):
                if task.deadline is None or task.deadline >= now:
                    continue
                await task_service.set_status(
                    task.task_id, TaskStatus.BLOCKED,
                    error=f"deadline passed ({task.deadline.isoformat()})")
                if self._events:
                    await self._events.publish(
                        "task.deadline_exceeded",
                        {"task_id": task.task_id, "deadline": task.deadline.isoformat()},
                        severity="warning",
                        project_id=task.project_id, agent_id=task.assigned_agent,
                        source="scheduler")
                # escalate to the agent's manager through the chain of command
                manager = "executive"
                if task.assigned_agent and agent_registry is not None:
                    agent = await agent_registry.get(task.assigned_agent)
                    if agent is not None and agent.parent_agent:
                        manager = agent.parent_agent
                if messages is not None:
                    try:
                        await messages.send_blocker(
                            task.assigned_agent or "scheduler", manager,
                            f"Task {task.task_id} ({task.title}) missed its "
                            f"deadline ({task.deadline.isoformat()}) and was "
                            "blocked. Reassign, reprioritize, or cancel.",
                            task_id=task.task_id, project_id=task.project_id)
                    except Exception:  # noqa: BLE001
                        logger.exception("deadline escalation message failed")
                escalated.append(task.task_id)
        return escalated
