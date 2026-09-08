"""Workspace Manager tests — isolated engineering workspaces.

Uses REAL git repositories (created in tmp dirs) so the worktree machinery is
exercised for real: isolation, parallel non-clobbering work, conflict
handling without data loss, merge-back, and discard.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest


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


@pytest.mark.asyncio
async def test_plain_workspace_when_no_repository(svc):
    record = await svc.workspaces.isolate("proj-x", task_id="t1", agent_id="alex.ceo")
    assert record.mode == "plain"
    assert Path(record.worktree_path).exists()
    (Path(record.worktree_path) / "note.txt").write_text("hello")
    result = await svc.workspaces.integrate(record.workspace_id)
    assert result["ok"] is True
    status = await svc.workspaces.status(record.workspace_id)
    assert status["status"] == "merged"
    await svc.workspaces.discard(record.workspace_id)
    assert not Path(record.worktree_path).exists()


@pytest.mark.asyncio
async def test_worktree_isolates_and_merges_back(svc, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    record = await svc.workspaces.isolate(
        "proj-x", task_id="t1", agent_id="backend-worker",
        repo_path=str(repo))
    assert record.mode == "worktree"
    assert Path(record.worktree_path).exists()
    # the worktree is a real git worktree of the repo
    assert "agentos" in _git(Path(record.worktree_path), "branch", "--show-current")
    # agent writes files inside the isolated worktree
    (Path(record.worktree_path) / "feature.py").write_text("print('hi')\n")
    (Path(record.worktree_path) / "README.md").write_text("# Project\nupdated\n")
    result = await svc.workspaces.integrate(record.workspace_id)
    assert result["ok"] is True, result
    assert result["merged"] is True
    assert result["commits"], "expected at least one commit"
    # the merge landed in the base repo
    assert (repo / "feature.py").exists()
    assert "updated" in (repo / "README.md").read_text()
    await svc.workspaces.discard(record.workspace_id)


@pytest.mark.asyncio
async def test_parallel_worktrees_do_not_clobber(svc, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    a = await svc.workspaces.isolate("proj-x", task_id="backend", agent_id="backend-worker",
                                     repo_path=str(repo))
    b = await svc.workspaces.isolate("proj-x", task_id="frontend", agent_id="frontend-worker",
                                     repo_path=str(repo))
    assert a.mode == "worktree" and b.mode == "worktree"
    assert a.worktree_path != b.worktree_path
    (Path(a.worktree_path) / "backend.py").write_text("backend\n")
    (Path(b.worktree_path) / "frontend.py").write_text("frontend\n")
    ma = await svc.workspaces.integrate(a.workspace_id)
    mb = await svc.workspaces.integrate(b.workspace_id)
    assert ma["ok"] is True and mb["ok"] is True
    assert (repo / "backend.py").exists()
    assert (repo / "frontend.py").exists()
    await svc.workspaces.discard(a.workspace_id)
    await svc.workspaces.discard(b.workspace_id)


@pytest.mark.asyncio
async def test_conflict_marks_failed_without_losing_work(svc, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    (repo / "app.py").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base app")
    a = await svc.workspaces.isolate("proj-x", task_id="a", agent_id="worker-a",
                                     repo_path=str(repo))
    b = await svc.workspaces.isolate("proj-x", task_id="b", agent_id="worker-b",
                                     repo_path=str(repo))
    (Path(a.worktree_path) / "app.py").write_text("change from A\n")
    (Path(b.worktree_path) / "app.py").write_text("change from B\n")
    ma = await svc.workspaces.integrate(a.workspace_id)
    assert ma["ok"] is True
    mb = await svc.workspaces.integrate(b.workspace_id)
    assert mb["ok"] is False
    assert mb.get("conflict") is True
    # no work lost: A's change is in the base, B's commits survive on its branch
    assert (repo / "app.py").read_text() == "change from A\n"
    status = await svc.workspaces.status(b.workspace_id)
    assert status["status"] == "failed"
    # B's branch still exists with its commit
    branch = b.branch
    commits = _git(repo, "rev-list", "--count", branch).strip()
    assert commits != "0"
    await svc.workspaces.discard(a.workspace_id)
    await svc.workspaces.discard(b.workspace_id)


@pytest.mark.asyncio
async def test_discard_removes_worktree_and_branch(svc, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    record = await svc.workspaces.isolate("proj-x", task_id="t1", agent_id="worker",
                                          repo_path=str(repo))
    branch = record.branch
    wt = Path(record.worktree_path)
    assert wt.exists()
    result = await svc.workspaces.discard(record.workspace_id)
    assert result["ok"] is True
    assert not wt.exists()
    branches = _git(repo, "branch", "--list", branch)
    assert branch not in branches


@pytest.mark.asyncio
async def test_harness_executes_task_inside_worktree(svc, tmp_path):
    """The coding harness runs the full agent runtime scoped to an isolated
    worktree and integrates the result — end to end with the echo model."""
    from agentos.domain.models import AgentDef

    repo = _make_repo(tmp_path / "repo")
    agent = AgentDef(id="test-worker", name="Test Worker", role="worker",
                     parent_agent="alex.ceo")
    await svc.agent_registry.create(agent)
    project = await svc.projects.create("iso", "isolated project")
    task = await svc.tasks.create(
        project.project_id, "isolated task",
        "Write a short status report and nothing else.",
        assigned_agent=agent.id)
    result = await svc.coding.execute(svc, agent, task, project,
                                      repo_path=str(repo))
    assert result.ok, result.error
    assert result.workspace_id
    record = await svc.workspaces.get(result.workspace_id)
    assert record is not None
    assert record.status.value in ("merged", "working")
    task_after = await svc.tasks.get(task.task_id)
    assert task_after.status.value == "completed"
    # the workspace was a real worktree
    assert record.mode == "worktree"
    await svc.workspaces.discard(record.workspace_id)