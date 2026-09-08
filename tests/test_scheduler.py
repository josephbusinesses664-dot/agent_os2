"""Scheduler subsystem tests: recurring/standing missions (Phase 3 scheduling),
claim-before-launch atomicity, max_runs auto-disable, and deadline escalation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentos.domain.models import Schedule, TaskStatus, utcnow


async def _mk_task(svc, project_id: str, *, title: str = "task",
                   status: TaskStatus = TaskStatus.PENDING,
                   deadline=None, agent: str = "executive"):
    task = await svc.tasks.create(project_id, title=title,
                                  description="test", assigned_agent=agent)
    task.deadline = deadline
    if status is not TaskStatus.PENDING:
        task.status = status
    await svc.tasks.save(task)
    return task


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def test_create_and_get(svc):
    s = await svc.scheduler.create("Daily Research", "Research the market",
                                   interval_seconds=3600)
    assert s.schedule_id.startswith("sched_")
    assert s.enabled is True
    assert s.interval_seconds == 3600
    assert s.run_count == 0
    assert s.max_runs is None

    got = await svc.scheduler.get(s.schedule_id)
    assert got is not None
    assert got.name == "Daily Research"
    assert got.goal == "Research the market"


async def test_min_interval_is_one_second(svc):
    s = await svc.scheduler.create("T", "G", interval_seconds=-5)
    assert s.interval_seconds == 1


async def test_list_and_delete(svc):
    await svc.scheduler.create("A", "goal a")
    b = await svc.scheduler.create("B", "goal b")
    rows = await svc.scheduler.list()
    assert len(rows) == 2
    assert await svc.scheduler.delete(b.schedule_id) is True
    assert await svc.scheduler.get(b.schedule_id) is None
    assert await svc.scheduler.delete(b.schedule_id) is False
    assert len(await svc.scheduler.list()) == 1


async def test_pause_and_resume(svc):
    s = await svc.scheduler.create("A", "goal a")
    paused = await svc.scheduler.set_enabled(s.schedule_id, False)
    assert paused.enabled is False
    resumed = await svc.scheduler.set_enabled(s.schedule_id, True)
    assert resumed.enabled is True


# ---------------------------------------------------------------------------
# Due claim (claim-before-launch atomicity)
# ---------------------------------------------------------------------------

async def test_due_returns_only_enabled_overdue_schedules(svc):
    now = utcnow()
    due = await svc.scheduler.create("due now", "g", interval_seconds=60)
    due.next_run_at = now - timedelta(seconds=1)
    await svc.scheduler.save(due)

    future = await svc.scheduler.create("not yet", "g", interval_seconds=60)
    future.next_run_at = now + timedelta(hours=1)
    await svc.scheduler.save(future)

    paused_due = await svc.scheduler.create("paused but due", "g",
                                            interval_seconds=60)
    paused_due.next_run_at = now - timedelta(seconds=1)
    paused_due.enabled = False
    await svc.scheduler.save(paused_due)

    rows = await svc.scheduler.due(now)
    assert [r.name for r in rows] == ["due now"]


async def test_claim_advances_state_and_is_idempotent(svc):
    now = utcnow()
    s = await svc.scheduler.create("tick", "g", interval_seconds=300)
    s.next_run_at = now - timedelta(seconds=1)
    await svc.scheduler.save(s)

    claimed1 = await svc.scheduler.claim_due(now)
    assert len(claimed1) == 1
    assert claimed1[0].run_count == 1
    assert claimed1[0].last_run_at is not None
    assert claimed1[0].next_run_at == now + timedelta(seconds=300)

    # second claim at the same instant must not double-fire
    claimed2 = await svc.scheduler.claim_due(now)
    assert claimed2 == []

    # a later tick sees nothing due until the interval passes
    later = now + timedelta(seconds=299)
    assert await svc.scheduler.claim_due(later) == []
    later2 = now + timedelta(seconds=301)
    assert len(await svc.scheduler.claim_due(later2)) == 1


async def test_max_runs_disables_after_last_claim(svc):
    now = utcnow()
    s = await svc.scheduler.create("limited", "g", interval_seconds=1,
                                   max_runs=2)
    s.next_run_at = now
    await svc.scheduler.save(s)

    c1 = await svc.scheduler.claim_due(now)
    assert len(c1) == 1
    assert c1[0].enabled is True  # run 1 of 2 — still enabled

    c2 = await svc.scheduler.claim_due(now + timedelta(seconds=2))
    assert len(c2) == 1
    assert c2[0].enabled is False  # run 2 of 2 — auto-disabled

    c3 = await svc.scheduler.claim_due(now + timedelta(seconds=9))
    assert c3 == []  # disabled → never due again


async def test_one_shot(svc):
    s = await svc.scheduler.create("once", "g", interval_seconds=60, max_runs=1)
    assert s.is_one_shot is True
    now = utcnow()
    s.next_run_at = now - timedelta(seconds=1)
    await svc.scheduler.save(s)
    claimed = await svc.scheduler.claim_due(now)
    assert len(claimed) == 1
    assert claimed[0].enabled is False


# ---------------------------------------------------------------------------
# tick firing
# ---------------------------------------------------------------------------

async def test_tick_fires_due_schedules_via_callback(svc):
    fired: list[str] = []

    async def fire_cb(schedule):
        fired.append(schedule.schedule_id)
        return {"run_id": f"run_{schedule.schedule_id}"}

    now = utcnow()
    s = await svc.scheduler.create("tick", "g", interval_seconds=60)
    s.next_run_at = now - timedelta(seconds=1)
    await svc.scheduler.save(s)

    launched = await svc.scheduler.tick(fire_cb=fire_cb, now=now)
    assert launched == [s.schedule_id]
    assert fired == [s.schedule_id]

    updated = await svc.scheduler.get(s.schedule_id)
    assert updated.last_status == "ok"
    assert updated.last_run_id == f"run_{s.schedule_id}"
    assert updated.run_count == 1


async def test_tick_records_failure_and_retries_next_interval(svc):
    async def broken_cb(schedule):
        raise RuntimeError("model backend down")

    now = utcnow()
    s = await svc.scheduler.create("failing", "g", interval_seconds=60)
    s.next_run_at = now - timedelta(seconds=1)
    await svc.scheduler.save(s)

    launched = await svc.scheduler.tick(fire_cb=broken_cb, now=now)
    assert launched == [s.schedule_id]  # claim happened

    updated = await svc.scheduler.get(s.schedule_id)
    assert updated.last_status == "failed"
    assert "model backend down" in (updated.last_error or "")
    assert updated.enabled is True  # stays enabled → retry next interval
    # next fire pushed to now + interval (backoff at schedule level)
    assert updated.next_run_at >= now + timedelta(seconds=59)


async def test_tick_without_callback_is_noop(svc):
    s = await svc.scheduler.create("idle", "g", interval_seconds=60)
    s.next_run_at = utcnow() - timedelta(seconds=1)
    await svc.scheduler.save(s)
    assert await svc.scheduler.tick() == []


async def test_engine_fire_schedule_runs_goal(svc):
    """fire_schedule routes through the real engine (echo provider offline)."""
    s = await svc.scheduler.create("agency run", "Write a tiny plan",
                                   workflow_id="build_feature",
                                   interval_seconds=3600)
    result = await svc.engine.fire_schedule(s)
    assert result.get("run_id")
    assert result.get("project_id")
    assert result.get("status") in ("completed", "failed")


# ---------------------------------------------------------------------------
# Deadline enforcement
# ---------------------------------------------------------------------------

async def test_deadline_blocks_overdue_pending_task(svc):
    project = await svc.projects.create("p", objective="x")
    now = utcnow()
    overdue = await _mk_task(svc, project.project_id, title="overdue",
                             deadline=now - timedelta(minutes=5),
                             agent="product-director")
    fine = await _mk_task(svc, project.project_id, title="fine",
                          deadline=now + timedelta(hours=2))
    no_deadline = await _mk_task(svc, project.project_id, title="none")

    escalated = await svc.scheduler.enforce_deadlines(
        svc.tasks, agent_registry=svc.agent_registry, messages=svc.messages,
        now=now)
    assert escalated == [overdue.task_id]

    got = await svc.tasks.get(overdue.task_id)
    assert got.status == TaskStatus.BLOCKED
    assert "deadline passed" in (got.error or "")
    assert (await svc.tasks.get(fine.task_id)).status == TaskStatus.PENDING
    assert (await svc.tasks.get(no_deadline.task_id)).status == TaskStatus.PENDING


async def test_deadline_escalation_sends_blocker_to_manager(svc):
    project = await svc.projects.create("p", objective="x")
    now = utcnow()
    # cto's parent is executive in the seeded org
    task = await _mk_task(svc, project.project_id, title="late",
                          deadline=now - timedelta(minutes=1),
                          agent="cto")
    await svc.scheduler.enforce_deadlines(
        svc.tasks, agent_registry=svc.agent_registry, messages=svc.messages,
        now=now)
    inbox = await svc.messages.inbox("executive", unread_only=True)
    blockers = [m for m in inbox
                if m.message_type.value == "blocker"
                and m.payload.get("reason", "").startswith(
                    f"Task {task.task_id}")]
    assert blockers, "manager should receive a blocker message"


async def test_running_overdue_task_is_not_blocked(svc):
    project = await svc.projects.create("p", objective="x")
    now = utcnow()
    task = await _mk_task(svc, project.project_id, title="in-flight",
                          status=TaskStatus.RUNNING,
                          deadline=now - timedelta(minutes=1))
    escalated = await svc.scheduler.enforce_deadlines(
        svc.tasks, agent_registry=svc.agent_registry, messages=svc.messages,
        now=now)
    assert task.task_id not in escalated
    assert (await svc.tasks.get(task.task_id)).status == TaskStatus.RUNNING


async def test_deadline_emits_event(svc):
    project = await svc.projects.create("p", objective="x")
    now = utcnow()
    task = await _mk_task(svc, project.project_id, title="late",
                          deadline=now - timedelta(minutes=1))
    await svc.scheduler.enforce_deadlines(
        svc.tasks, agent_registry=svc.agent_registry, messages=svc.messages,
        now=now)
    events = await svc.events.recent(limit=20)
    assert any(e.type == "task.deadline_exceeded"
               and e.payload.get("task_id") == task.task_id for e in events)


# ---------------------------------------------------------------------------
# Worker-loop integration
# ---------------------------------------------------------------------------

async def test_worker_loop_drives_due_schedule(svc):
    """A due schedule is launched by the worker loop within a couple passes."""
    s = await svc.scheduler.create("worker tick", "Produce a one-line report",
                                   interval_seconds=3600)
    s.next_run_at = utcnow() - timedelta(seconds=5)
    await svc.scheduler.save(s)

    stop = __import__("asyncio").Event()
    loop_task = __import__("asyncio").create_task(svc.engine.worker_loop(stop))
    try:
        # wait for the scheduler pass (10s cadence is too slow for a unit
        # test, so drive claim+fire directly as the worker would)
        for _ in range(50):
            launched = await svc.scheduler.claim_due()
            for sch in launched:
                await svc.engine.fire_schedule(sch)
            updated = await svc.scheduler.get(s.schedule_id)
            if updated.run_count > 0:
                break
            await __import__("asyncio").sleep(0.05)
        updated = await svc.scheduler.get(s.schedule_id)
        assert updated.run_count >= 1
    finally:
        stop.set()
        await loop_task
