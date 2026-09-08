"""Orchestrator-level workspace isolation tests.

With WORKSPACE_ISOLATION=true, coding stages execute inside their own git
worktree cut from the project workspace; the work merges back when the stage
finishes. Plain-dir projects are synced back instead. Failures never lose the
worktree contents.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentos.domain.models import WorkflowDef, WorkflowStage


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@agentos.local")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# Project\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    return path


def _iso_workflow(stage_id: str = "review", agent: str = "executive") -> WorkflowDef:
    stage = WorkflowStage(stage_id=stage_id, name="Review", agent_role=agent,
                          description="Write the review.")
    return WorkflowDef(workflow_id="iso", name="iso", entry_stage=stage_id,
                       stages=[stage])


async def _run_stage(svc, project, stage_id="review", agent="executive"):
    workflow = _iso_workflow(stage_id, agent)
    return await svc.engine.execute_stage_work(workflow, workflow.stages[0],
                                               project, "run_iso")


@pytest.mark.asyncio
async def test_isolation_off_runs_in_shared_workspace(svc):
    project = await svc.projects.create("iso-off", "objective")
    result = await _run_stage(svc, project)
    assert result.status == "completed", result.error
    records = await svc.workspaces.list()
    assert records == [], "no workspaces when isolation is off"
    canonical = svc.workspace / project.project_id
    assert (canonical / "artifacts" / "echo-summary.md").exists()


@pytest.mark.asyncio
async def test_isolation_worktree_merges_back_to_repo(svc):
    svc.settings.workspace_isolation = True
    project = await svc.projects.create("iso-wt", "objective")
    canonical = svc.workspace / project.project_id
    _make_repo(canonical)

    result = await _run_stage(svc, project)
    assert result.status == "completed", result.error

    records = await svc.workspaces.list()
    assert records and records[0].mode == "worktree"
    assert records[0].status.value == "merged"
    # the merged deliverable landed in the repo's branch
    assert (canonical / "artifacts" / "echo-summary.md").exists()
    assert "agentos/" in records[0].branch
    await svc.workspaces.discard(records[0].workspace_id)


@pytest.mark.asyncio
async def test_isolation_plain_dir_synced_back(svc):
    svc.settings.workspace_isolation = True
    project = await svc.projects.create("iso-plain", "objective")
    canonical = svc.workspace / project.project_id
    canonical.mkdir(parents=True, exist_ok=True)  # no git repo

    result = await _run_stage(svc, project)
    assert result.status == "completed", result.error

    records = await svc.workspaces.list()
    assert records and records[0].mode == "plain"
    assert records[0].status.value == "merged"
    # plain sandboxes sync their deliverables back to the shared workspace
    assert (canonical / "artifacts" / "echo-summary.md").exists()


@pytest.mark.asyncio
async def test_isolation_preserves_work_on_failure(svc):
    """A failed build stage must not lose the isolated work: the worktree is
    integrated (commits preserved) even when the stage itself fails."""
    svc.settings.workspace_isolation = True
    project = await svc.projects.create("iso-fail", "objective")
    canonical = svc.workspace / project.project_id
    _make_repo(canonical)

    # implement-frontend requires real HTML in the answer; the offline echo
    # model cannot produce it, so the stage fails — but the worktree merges.
    result = await _run_stage(svc, project, stage_id="implement-frontend",
                              agent="frontend-lead")
    assert result.status == "failed"
    records = await svc.workspaces.list()
    assert records, "an isolated workspace must exist"
    assert records[0].status.value in ("merged", "failed")
    assert records[0].mode == "worktree"
    # whatever the agent wrote in the worktree reached the repo (the echo
    # fallback artifact), so no work is stranded
    assert (canonical / "artifacts" / "echo-summary.md").exists()
    await svc.workspaces.discard(records[0].workspace_id)