"""Tests for the live-run debrief fixes (AGENT-DEBRIEF.md).

Covers the code-level fixes extracted from the Mattermost run debrief:
tool aliases + closest-match errors, per-project inbox scoping (no cross-run
state confusion), a fail-closed executive GO/NO-GO gate, guaranteed stage
artifacts, and worker duplicate-task guarding.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.domain.models import TaskStatus


# ---------------------------------------------------------------------------
# Cluster B — model hallucinated tool names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_aliases_resolve_to_real_tools(svc):
    tool = await svc.tool_registry.get("repo_tree")
    assert tool is not None and tool.name == "repo.tree"
    assert svc.tool_registry.handler("repo_tree") is not None
    for alias, real in (("project_state", "project.state"),
                        ("filesystem_write", "filesystem.write"),
                        ("arch.tree", "repo.tree"),
                        ("git_status", "git.status")):
        t = await svc.tool_registry.get(alias)
        assert t is not None and t.name == real, f"{alias} -> {real}"


@pytest.mark.asyncio
async def test_alias_executes_end_to_end(svc):
    project = await svc.projects.create("alias", "alias project")
    task = await svc.tasks.create(project.project_id, "alias task",
                                  assigned_agent="executive")
    agent = await svc.agent_registry.get("executive")
    ctx = svc.runtime(agent, task, project).ctx
    result = await svc.executor.execute(ctx, agent, "repo_tree", {"depth": 1})
    assert result.get("ok") is True, result


@pytest.mark.asyncio
async def test_unknown_tool_suggests_closest_match(svc):
    project = await svc.projects.create("suggest", "suggest project")
    task = await svc.tasks.create(project.project_id, "suggest task",
                                  assigned_agent="executive")
    agent = await svc.agent_registry.get("executive")
    ctx = svc.runtime(agent, task, project).ctx
    result = await svc.executor.execute(ctx, agent, "repo_tre", {})
    assert not result.get("ok")
    assert "did you mean" in result.get("error", "")
    assert "repo.tree" in result.get("error", "")


# ---------------------------------------------------------------------------
# Cluster G — cross-run state confusion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbox_context_scoped_to_current_project(svc):
    project_a = await svc.projects.create("proj-a", "project A")
    project_b = await svc.projects.create("proj-b", "project B")
    await svc.messages.send("handoff", "other-agent", "executive",
                            {"note": "HANDOFF_FROM_A"},
                            project_id=project_a.project_id)
    await svc.messages.send("handoff", "other-agent", "executive",
                            {"note": "HANDOFF_FROM_B"},
                            project_id=project_b.project_id)
    agent = await svc.agent_registry.get("executive")
    task = await svc.tasks.create(project_a.project_id, "task in A",
                                  assigned_agent=agent.id)
    runtime = svc.runtime(agent, task, project_a)
    system, _user = await runtime._build_prompt(task)
    assert "HANDOFF_FROM_A" in system
    assert "HANDOFF_FROM_B" not in system


# ---------------------------------------------------------------------------
# Debrief item 2 — the executive GO/NO-GO gate must fail closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executive_gate_fails_closed_without_evidence(svc):
    from agentos.orchestration.graph import _executive_gate

    project = await svc.projects.create("gate", "gate project")
    engine = type("E", (), {"svc": svc})()
    verdict, rationale = await _executive_gate(engine, {"stage_results": {}},
                                               project)
    assert verdict == "nogo"
    assert "no evidence" in rationale


@pytest.mark.asyncio
async def test_executive_gate_fails_closed_when_model_unreachable(svc):
    from agentos.orchestration.graph import _executive_gate

    class BrokenProvider:
        def is_configured(self):
            return True

        async def complete(self, request):
            raise RuntimeError("simulated outage")

    svc.providers["echo"] = BrokenProvider()
    project = await svc.projects.create("gate2", "gate project")
    engine = type("E", (), {"svc": svc})()
    state = {"stage_results": {"research": {"output": "real evidence here"}}}
    verdict, rationale = await _executive_gate(engine, state, project)
    assert verdict == "nogo"
    assert "gate call failed" in rationale


# ---------------------------------------------------------------------------
# Cluster H — planned artifacts are guaranteed to land on disk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_carries_artifact_prefix(svc):
    workflow = svc.planner.build_workflow("artifact guarantee", stages=["understanding"])
    assert workflow.stages[0].artifact_prefix == "artifacts/understanding.md"


@pytest.mark.asyncio
async def test_stage_artifact_always_written(svc):
    workflow = svc.planner.build_workflow("artifact guarantee",
                                          stages=["understanding"])
    stage = workflow.stages[0]
    project = await svc.projects.create("art-proj", "artifact project")
    result = await svc.engine.execute_stage_work(workflow, stage, project, "run_x")
    assert result.status == "completed", result.error
    artifact = svc.workspace / project.project_id / "artifacts" / "understanding.md"
    assert artifact.exists(), "stage deliverable must land on disk"
    assert artifact.stat().st_size >= 200, "stub artifacts get replaced with real output"


# ---------------------------------------------------------------------------
# Cluster I — duplicate/parallel stage executions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_skips_duplicate_queued_task(svc):
    project = await svc.projects.create("dup", "duplicate project")
    task = await svc.tasks.create(project.project_id, "dup task",
                                  assigned_agent="executive")
    await svc.tasks.set_status(task.task_id, TaskStatus.RUNNING)  # already being worked
    await svc.queue.enqueue({"task_id": task.task_id})

    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.4)
        stop.set()

    await asyncio.gather(svc.engine.worker_loop(stop), stopper())
    after = await svc.tasks.get(task.task_id)
    assert after.status == TaskStatus.RUNNING, "already-running task must not be re-run"