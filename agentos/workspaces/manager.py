"""Workspace Manager — isolated engineering workspaces (parallel software
engineering, master spec §10/§11).

Each coding task can run in its OWN git worktree cut from the project repo,
so many agents work simultaneously without clobbering each other's files.
When the project has no repository, the workspace degrades to a plain
sandbox directory (still isolated, just no merge-back).

Lifecycle:
    isolate()   → create the worktree / sandbox dir, record it
    status()    → git status inside the workspace
    diff()      → git diff against the base branch
    integrate() → commit the workspace's work, merge it back to the base
                  branch (conflicts mark the workspace failed, never lose work)
    discard()   → remove the worktree + branch (or the sandbox dir)

The manager is deliberately storage-agnostic: records live in the same
EntityStore as every other organizational object, git is invoked through
`asyncio` subprocesses with no shell (no injection surface), and every step
records what actually happened.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agentos.db.store import EntityStore
from agentos.domain.models import WorkspaceRecord, WorkspaceStatus, new_id

logger = logging.getLogger("agentos.workspaces")

_COLLECTION = "workspaces"


def _sanitize_branch(name: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9._/-]", "-", name)[:60] or "task"


class WorkspaceManager:
    def __init__(self, store: EntityStore, worktrees_root: Path) -> None:
        self.store = store
        self.worktrees_root = Path(worktrees_root).resolve()
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def isolate(self, project_id: str, task_id: str = "",
                      agent_id: str = "", repo_path: Optional[str] = None) -> WorkspaceRecord:
        """Create an isolated workspace for one task.

        `repo_path` defaults to the project's sandbox directory. If that
        directory is a git repository (with at least one commit) the workspace
        is a real worktree; otherwise a plain isolated directory.
        """
        record = WorkspaceRecord(
            project_id=project_id, task_id=task_id, agent_id=agent_id,
        )
        candidate = Path(repo_path).resolve() if repo_path else \
            self.worktrees_root.parent / project_id
        record.repo_path = str(candidate)
        worktree = self.worktrees_root / record.workspace_id
        if await self._is_git_repo(candidate) and await self._has_commits(candidate):
            base_branch = await self._git(candidate, ["branch", "--show-current"])
            base_branch = (base_branch or "").strip() or "HEAD"
            branch = f"agentos/{_sanitize_branch(task_id or record.workspace_id)}"
            created = await self._git(
                candidate, ["worktree", "add", "-b", branch, str(worktree)])
            if created is None:
                # branch already exists (e.g. a retried task) — attach to it
                created = await self._git(
                    candidate, ["worktree", "add", str(worktree), branch])
            if created is None:
                # worktree machinery refused; degrade to a plain sandbox
                worktree.mkdir(parents=True, exist_ok=True)
                record.mode = "plain"
                record.note = "git worktree unavailable — plain sandbox used"
            else:
                record.mode = "worktree"
                record.branch = branch
                record.base_branch = base_branch
                record.status = WorkspaceStatus.WORKING
        else:
            worktree.mkdir(parents=True, exist_ok=True)
            record.mode = "plain"
            record.status = WorkspaceStatus.WORKING
            record.note = "no git repository — plain isolated directory"
        record.worktree_path = str(worktree)
        record.touch()
        await self.store.save(_COLLECTION, record)
        return record

    async def integrate(self, workspace_id: str, message: str = "",
                        operator: str = "agent") -> dict:
        """Commit the workspace's work and merge it back to the base branch.

        Returns a dict with `ok`, the merge details, and any conflict
        information. Conflicts NEVER lose work: the workspace stays intact,
        the branch keeps its commits, and the record is marked failed so a
        lead/human can resolve.
        """
        record = await self.get(workspace_id)
        if record is None:
            raise KeyError(f"workspace {workspace_id} not found")
        if record.status in (WorkspaceStatus.MERGED, WorkspaceStatus.DISCARDED):
            return {"ok": False, "error": f"workspace already {record.status.value}",
                    "workspace_id": workspace_id}
        if record.mode != "worktree":
            record.status = WorkspaceStatus.MERGED
            record.note = "no repository — nothing to merge"
            record.touch()
            await self.store.save(_COLLECTION, record)
            return {"ok": True, "mode": "plain", "merged": False,
                    "note": record.note, "workspace_id": workspace_id}

        repo = Path(record.repo_path)
        wt = Path(record.worktree_path)
        commits: list[str] = []
        changed = await self._git(wt, ["status", "--porcelain"])
        if changed is not None and changed.strip():
            await self._git(wt, ["add", "-A"])
            author = f"agent:{record.agent_id or 'unknown'} <agent@agentos.local>"
            head = await self._git(
                wt, ["commit", "-m",
                     message or f"work from workspace {record.workspace_id}",
                     "--author", author])
            commit_hash = await self._git(wt, ["rev-parse", "HEAD"])
            if commit_hash:
                commits.append(commit_hash.strip())
        elif record.branch:
            # no working changes; check whether the branch has commits anyway
            ahead = await self._git(
                repo, ["rev-list", "--count", f"{record.base_branch}..{record.branch}"])
            if ahead is not None and ahead.strip() and ahead.strip() != "0":
                commit_hash = await self._git(repo, ["rev-parse", record.branch])
                if commit_hash:
                    commits.append(commit_hash.strip())

        record.commits.extend(commits)
        if not commits:
            record.status = WorkspaceStatus.MERGED
            record.note = "no changes to merge"
            record.touch()
            await self.store.save(_COLLECTION, record)
            return {"ok": True, "merged": False, "commits": [],
                    "note": record.note, "workspace_id": workspace_id}

        # merge back to the base branch
        if record.base_branch != "HEAD":
            checked = await self._git(repo, ["checkout", record.base_branch])
            if checked is None:
                record.status = WorkspaceStatus.FAILED
                record.note = f"could not check out base branch {record.base_branch}"
                record.touch()
                await self.store.save(_COLLECTION, record)
                return {"ok": False, "error": record.note, "workspace_id": workspace_id}
        merge = await self._git(
            repo, ["merge", "--no-ff", "-m",
                   f"merge {record.branch} (workspace {record.workspace_id})",
                   record.branch])
        if merge is None:
            await self._git(repo, ["merge", "--abort"])
            record.status = WorkspaceStatus.FAILED
            record.note = (f"merge conflict merging {record.branch} — branch and "
                           f"commits preserved for manual resolution")
            record.touch()
            await self.store.save(_COLLECTION, record)
            return {"ok": False, "error": record.note, "workspace_id": workspace_id,
                    "conflict": True, "commits": commits}
        merged_commit = await self._git(repo, ["rev-parse", "HEAD"])
        record.status = WorkspaceStatus.MERGED
        record.note = "merged to " + record.base_branch
        record.touch()
        await self.store.save(_COLLECTION, record)
        return {"ok": True, "merged": True, "commits": commits,
                "merge_commit": (merged_commit or "").strip(),
                "note": record.note, "workspace_id": workspace_id}

    async def discard(self, workspace_id: str, operator: str = "agent") -> dict:
        """Remove the workspace and its branch/sandbox. Work merged earlier is
        preserved in the base branch; unmerged commits are lost (operator
        acknowledges by calling discard explicitly)."""
        record = await self.get(workspace_id)
        if record is None:
            raise KeyError(f"workspace {workspace_id} not found")
        if record.mode == "worktree" and Path(record.repo_path).exists():
            await self._git(Path(record.repo_path),
                            ["worktree", "remove", "--force", record.worktree_path])
            if record.branch:
                await self._git(Path(record.repo_path),
                                ["branch", "-D", record.branch])
        shutil.rmtree(record.worktree_path, ignore_errors=True)
        record.status = WorkspaceStatus.DISCARDED
        record.note = f"discarded by {operator}"
        record.touch()
        await self.store.save(_COLLECTION, record)
        return {"ok": True, "workspace_id": workspace_id, "status": "discarded"}

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    async def get(self, workspace_id: str) -> Optional[WorkspaceRecord]:
        return await self.store.get(_COLLECTION, workspace_id, WorkspaceRecord)

    async def list(self) -> list[WorkspaceRecord]:
        records = await self.store.list(_COLLECTION, WorkspaceRecord)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    async def active_for_task(self, task_id: str) -> Optional[WorkspaceRecord]:
        for record in await self.list():
            if record.task_id == task_id and record.status in (
                    WorkspaceStatus.ISOLATED, WorkspaceStatus.WORKING):
                return record
        return None

    async def status(self, workspace_id: str) -> dict:
        record = await self.get(workspace_id)
        if record is None:
            raise KeyError(f"workspace {workspace_id} not found")
        out = {"workspace_id": workspace_id, "mode": record.mode,
               "status": record.status.value, "branch": record.branch,
               "base_branch": record.base_branch, "task_id": record.task_id,
               "agent_id": record.agent_id, "commits": record.commits,
               "note": record.note}
        if record.mode == "worktree" and Path(record.worktree_path).exists():
            porcelain = await self._git(Path(record.worktree_path),
                                        ["status", "--porcelain"])
            out["dirty_files"] = [l for l in (porcelain or "").splitlines() if l.strip()]
            out["worktree_exists"] = True
        else:
            out["worktree_exists"] = Path(record.worktree_path).exists()
        return out

    async def diff(self, workspace_id: str, stat: bool = True) -> str:
        record = await self.get(workspace_id)
        if record is None:
            raise KeyError(f"workspace {workspace_id} not found")
        if record.mode != "worktree":
            return "no repository — no diff"
        wt = Path(record.worktree_path)
        args = ["diff", f"{record.base_branch}...HEAD"] if record.base_branch != "HEAD" \
            else ["diff"]
        if stat:
            args.append("--stat")
        out = await self._git(wt, args)
        return out or ""

    # ------------------------------------------------------------------
    # Git plumbing
    # ------------------------------------------------------------------
    async def _git(self, cwd: Path, args: list[str]) -> Optional[str]:
        """Run git without a shell. Returns stdout, or None on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(cwd), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                logger.debug("git %s in %s failed: %s", args, cwd,
                             stderr.decode(errors="replace")[:300])
                return None
            return stdout.decode(errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.debug("git %s in %s raised: %s", args, cwd, exc)
            return None

    async def _is_git_repo(self, path: Path) -> bool:
        if not path.exists():
            return False
        out = await self._git(path, ["rev-parse", "--is-inside-work-tree"])
        return out is not None and out.strip() == "true"

    async def _has_commits(self, path: Path) -> bool:
        out = await self._git(path, ["rev-parse", "HEAD"])
        return out is not None and bool(out.strip())