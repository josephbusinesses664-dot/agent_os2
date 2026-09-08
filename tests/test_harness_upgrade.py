"""Harness upgrade tests (master prompt §3/§5/§14/§19):

- verify → repair loop: an unverified run gets bounded repair iterations
  instead of ending when the model says "done"
- explicit VERIFYING task state while evidence checks run
- context compaction keeps long tool sessions inside the model budget
"""

from __future__ import annotations

import pytest

from agentos.agents.runtime import (
    CONTEXT_BUDGET_CHARS,
    AgentRuntime,
    MAX_REPAIR_ROUNDS,
)
from agentos.domain.models import Task, TaskStatus


@pytest.mark.asyncio
async def test_unverified_run_gets_bounded_repair_rounds(svc):
    """A run whose tools all fail must enter the verify→repair loop and
    exhaust its bounded repair budget rather than silently 'completing'."""
    agent = await svc.agent_registry.get("product-researcher")
    project = await svc.projects.create("P", "x")
    # planned tool call targets a nonexistent file → tool fails → verification
    # fails → the repair loop should kick in (and stay honest about it)
    task = Task(task_id="t-repair", project_id=project.project_id,
                title="Research",
                description="Investigate the market and write a report. "
                            "PLANNED_TOOL_CALLS: [{\"tool\": \"filesystem.read\", "
                            "\"args\": {\"path\": \"does-not-exist.txt\"}}]")
    run = svc.runtime(agent, task, project)
    outcome = await run.run(task)
    # verification failed (no artifacts, failed tool), repair iterations ran
    assert outcome.repairs >= 1
    assert outcome.repairs <= MAX_REPAIR_ROUNDS
    # the run stayed honest: it is not marked verified
    assert outcome.verified is False
    assert "FAILED" in (outcome.verification_note or "") or "failed" in (
        outcome.verification_note or "")


@pytest.mark.asyncio
async def test_repair_events_published(svc):
    """Each repair iteration publishes an agent.repair event for observability."""
    agent = await svc.agent_registry.get("test-engineer")
    project = await svc.projects.create("P", "x")
    task = Task(task_id="t-repair-evt", project_id=project.project_id,
                title="Check",
                description="Verify something. "
                            "PLANNED_TOOL_CALLS: [{\"tool\": \"filesystem.read\", "
                            "\"args\": {\"path\": \"nope.txt\"}}]")
    run = svc.runtime(agent, task, project)
    await run.run(task)
    events = await svc.events.recent(limit=50, event_type="agent.repair")
    assert len(events) >= 1
    assert events[0].payload["attempt"] >= 1


@pytest.mark.asyncio
async def test_verifying_state_used_during_run(svc):
    """The task must pass through the explicit VERIFYING state during a run."""
    agent = await svc.agent_registry.get("documentation-agent")
    project = await svc.projects.create("P", "x")
    task = await svc.tasks.create(
        project.project_id, "Write doc", assigned_agent=agent.id,
        description="Write artifacts/doc.md. "
                    "PLANNED_TOOL_CALLS: [{\"tool\": \"filesystem.write\", "
                    "\"args\": {\"path\": \"artifacts/doc.md\", \"content\": \"AUTO\"}}]")
    outcome = await svc.engine.run_single_task(task.task_id)
    assert outcome.error is None
    task = await svc.tasks.get(task.task_id)
    assert task.status == TaskStatus.COMPLETED
    # the run must have passed through the explicit VERIFYING state — the
    # task service publishes a status_changed event on every transition
    transitions = [e.payload for e in await svc.events.recent(
        limit=100, event_type="task.status_changed")
        if e.task_id == task.task_id]
    assert transitions, "no status transitions recorded"
    assert any(p["to"] == "verifying" for p in transitions), transitions


def test_context_compaction_trims_old_tool_results():
    """Long tool sessions must be condensed, not dumped wholesale into the
    next model call (§19 context budget)."""
    messages = [{"role": "user", "content": "task"}]
    big_result = "[tool result for web.search]\n" + ("x" * (CONTEXT_BUDGET_CHARS * 2))
    messages.append({"role": "user", "content": big_result})
    messages.append({"role": "user", "content": "[tool result for web.search]\n" + ("y" * 500)})
    total_before = sum(len(m["content"]) for m in messages)
    assert total_before > CONTEXT_BUDGET_CHARS

    compressed = AgentRuntime._compact_context(messages)
    assert compressed >= 1
    total_after = sum(len(m["content"]) for m in messages)
    # the oldest huge transcript was trimmed to a one-line pointer
    assert total_after < total_before
    assert "(transcript trimmed" in messages[1]["content"]
    # the newest transcript is untouched
    assert "y" * 500 in messages[2]["content"]