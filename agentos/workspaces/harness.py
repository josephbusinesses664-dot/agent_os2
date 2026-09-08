"""Coding harness interface (master spec §10).

The organizational layer owns task / permissions / context / budget /
lifecycle / evaluation. The coding harness owns the repository mechanics:
an isolated workspace, the coding loop, tests, and the merge back.

This is an extension point, not a monolith: `CodingHarness` is the contract
any runtime (local git worktrees today; OpenCode / OpenHands / Claude Code /
Codex connectors later) implements, and the rest of the OS never depends on
how a workspace is created — only that `prepare` / `execute` / `verify` /
`integrate` exist.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional, Protocol

from agentos.domain.models import AgentDef, Project, Task, TaskStatus
from agentos.workspaces.manager import WorkspaceManager

logger = logging.getLogger("agentos.harness")


class CodingRunResult:
    def __init__(self, *, workspace_id: str, ok: bool = False, content: str = "",
                 artifacts: Optional[list[str]] = None, model: str = "",
                 cost: float = 0.0, error: str = "", merge: Optional[dict] = None) -> None:
        self.workspace_id = workspace_id
        self.ok = ok
        self.content = content
        self.artifacts = artifacts or []
        self.model = model
        self.cost = cost
        self.error = error
        self.merge = merge or {}

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class CodingHarness(Protocol):
    """Contract every coding runtime implements."""

    name: str

    async def prepare(self, svc: Any, agent: AgentDef, task: Task,
                      project: Optional[Project],
                      repo_path: Optional[str] = None) -> Any:
        """Create an isolated workspace for the task."""

    async def execute(self, svc: Any, agent: AgentDef, task: Task,
                      project: Optional[Project],
                      repo_path: Optional[str] = None) -> CodingRunResult:
        """Run the task's work inside the isolated workspace."""

    async def verify(self, svc: Any, workspace: Any,
                     command: Optional[str] = None) -> dict:
        """Check the workspace's state (tests/status) before integration."""

    async def integrate(self, svc: Any, workspace: Any,
                        message: str = "") -> dict:
        """Merge the workspace's work back into the shared tree."""


class LocalCodingHarness:
    """Runs the built-in agent runtime inside a git-worktree workspace.

    The agent's filesystem writes land in the worktree (the RuntimeContext
    workspace is overridden to the worktree path), so parallel agents never
    clobber each other, and `integrate` merges the finished branch back.
    """

    name = "local-git-worktree"

    def __init__(self, workspaces: WorkspaceManager) -> None:
        self.workspaces = workspaces

    async def prepare(self, svc: Any, agent: AgentDef, task: Task,
                      project: Optional[Project],
                      repo_path: Optional[str] = None) -> Any:
        return await self.workspaces.isolate(
            project_id=task.project_id, task_id=task.task_id,
            agent_id=agent.id, repo_path=repo_path)

    async def execute(self, svc: Any, agent: AgentDef, task: Task,
                      project: Optional[Project],
                      repo_path: Optional[str] = None) -> CodingRunResult:
        workspace = await self.prepare(svc, agent, task, project, repo_path)
        await svc.tasks.set_status(task.task_id, TaskStatus.RUNNING)
        try:
            run = svc.runtime(agent, task, project,
                              workspace_path=workspace.worktree_path)
            outcome = await run.run(task)
            result = CodingRunResult(
                workspace_id=workspace.workspace_id, ok=not outcome.error,
                content=outcome.content or "", artifacts=outcome.artifacts,
                model=outcome.model, cost=outcome.cost, error=outcome.error or "")
            if outcome.error:
                await svc.tasks.set_status(task.task_id, TaskStatus.FAILED,
                                           error=outcome.error)
            else:
                result.merge = await self.workspaces.integrate(
                    workspace.workspace_id, operator=agent.id)
                await svc.tasks.set_status(task.task_id, TaskStatus.COMPLETED,
                                           result=outcome.content[:4000])
                for artifact in outcome.artifacts:
                    await svc.tasks.record_artifact(task.task_id, artifact)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("harness execute failed")
            return CodingRunResult(workspace_id=workspace.workspace_id,
                                   ok=False, error=str(exc))

    async def verify(self, svc: Any, workspace: Any,
                     command: Optional[str] = None) -> dict:
        """Check the workspace before integration: git status, and optionally
        run a test command inside the worktree (real execution, no faking)."""
        report = await self.workspaces.status(workspace.workspace_id)
        if command and Path(workspace.worktree_path).exists():
            try:
                proc = await asyncio.create_subprocess_shell(
                    command, cwd=workspace.worktree_path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
                report["tests"] = {"command": command, "returncode": proc.returncode,
                                   "stdout": stdout.decode(errors="replace")[-2000:],
                                   "stderr": stderr.decode(errors="replace")[-2000:]}
            except Exception as exc:  # noqa: BLE001
                report["tests"] = {"command": command, "error": str(exc)}
        return report

    async def integrate(self, svc: Any, workspace: Any,
                        message: str = "") -> dict:
        return await self.workspaces.integrate(workspace.workspace_id,
                                               message=message, operator="harness")