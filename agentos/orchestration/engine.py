"""Orchestration Engine.

The engine drives workflows through the LangGraph state machine, executes
individual stages (instantiating the assigned agent for the duration of the
stage), enforces spawning limits, records usage/cost and handles approvals,
retries and failure recovery.

Spawning limits (defense against agent storms):
  * max recursion depth per agent chain
  * max parallel agents
  * budget checks per task
  * duplicate-task detection while a task is active
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from agentos.agents.runtime import AgentRunResult
from agentos.domain.models import (
    AgentStatus,
    ApprovalStatus,
    BudgetScope,
    Project,
    StageResult,
    Task,
    TaskStatus,
    UsageRecord,
    WorkflowDef,
    WorkflowStage,
    new_id,
)
from agentos.orchestration.state import new_state
from agentos.services import Services

logger = logging.getLogger("agentos.engine")

TRANSIENT_CLASSES = {"transient", "provider"}

# Coding stages run in their own isolated git worktree when
# WORKSPACE_ISOLATION=true — the deliverable merges back to the project
# workspace after the stage finishes.
_ISOLATED_STAGES = {"implement-frontend", "implement-backend", "testing",
                    "security", "review"}


class SpawnLimitError(Exception):
    pass


class OrchestratorEngine:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self._graph = None
        self._active_runs: dict[str, dict] = {}
        self._active_task_hashes: dict[str, str] = {}  # task hash -> task id
        self._parallel_count = 0

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    async def execute_goal(self, goal: str, user_id: str = "human",
                           workflow_id: Optional[str] = None,
                           project_name: Optional[str] = None,
                           home_channel: str = "") -> dict:
        """Executive flow: create a project and run a workflow on it.

        By default the executive plans dynamically (keyword-scored stages,
        filtered by the branch directors) instead of blindly running the full
        0→100 pipeline. Pass a workflow_id to pin a fixed workflow."""
        if workflow_id is None:
            result = await self.execute_dynamic(goal, user_id=user_id,
                                                home_channel=home_channel)
            result["project_name"] = result.get("project_name", "")
            return result
        name = project_name or _slug(goal)[:60] or "New Project"
        project = await self.svc.projects.create(name, objective=goal, created_by=user_id,
                                                 workflow_id=workflow_id)
        await self.svc.events.publish("project.created", {"name": name, "goal": goal},
                                      project_id=project.project_id, source="executive")
        run = await self.run_workflow(workflow_id, project.project_id)
        return {"project_id": project.project_id, "run_id": run["run_id"],
                "status": run["status"]}

    async def execute_dynamic(self, goal: str, user_id: str = "human",
                              stages: Optional[list[str]] = None,
                              expand_full: bool = False,
                              home_channel: str = "") -> dict:
        """Autonomous planning: the planner selects the required stages from
        the goal (explicit PLANNED_STAGES marker, keyword scoring, or the full
        pipeline on request), then runs them through the same engine."""
        from agentos.planning import _parse_marker
        chosen = stages
        if chosen is None and not expand_full and not _parse_marker(goal):
            # the executive plans, then each branch director decides which of
            # their team's stages are actually needed for this goal
            chosen = await self._director_filter(
                goal, self.svc.planner.plan_stages(goal))
        plan = self.svc.planner.plan_summary(goal) if stages is None else {
            "stages": stages, "count": len(stages)}
        workflow = self.svc.planner.build_workflow(goal, stages=chosen) \
            if chosen is not None else self.svc.planner.build_workflow(goal)
        if expand_full and stages is None:
            workflow = self.svc.planner.build_workflow(
                goal, stages=self.svc.planner.plan_stages(goal, expand_full=True))
        name = _slug(goal)[:60] or "New Project"
        project = await self.svc.projects.create(
            name, objective=goal, created_by=user_id, workflow_id=workflow.workflow_id)
        mm = getattr(self.svc, "mattermost", None)
        if mm is not None and home_channel:
            # registered BEFORE the run so stage checkpoints can mirror home
            mm._project_channels[project.project_id] = home_channel
        await self.svc.events.publish(
            "workflow.planned",
            {"goal": goal[:200], "stages": plan["stages"],
             "agents": plan.get("agents", []), "workflow": workflow.workflow_id},
            project_id=project.project_id, source="executive")
        run = await self.run_workflow_obj(workflow, project.project_id)
        return {"project_id": project.project_id, "run_id": run["run_id"],
                "status": run["status"], "planned_stages": plan["stages"]}

    _BRANCH_DIRECTORS = {
        "executive": "executive", "product": "product-director",
        "engineering": "cto", "design": "design-director",
        "research": "research-director", "marketing": "marketing-director",
        "sales": "sales-director", "qa": "qa-director",
        "operations": "operations-director",
    }

    async def _director_filter(self, goal: str, stages: list[str]) -> list[str]:
        """Hierarchy gate: each branch director keeps only the stages their
        team genuinely needs for this goal. The executive plans; directors
        decide who inside their branch gets activated. Never breaks a run —
        on any failure the planned stages are kept."""
        import re as _re

        from agentos.agents.hierarchy import ORG_AGENTS
        from agentos.agents.personas import BRANCH_OF
        from agentos.domain.models import ModelRequest

        kept = list(stages)
        templates = self.svc.planner._load_templates()
        branch_stages: dict[str, list[str]] = {}
        for sid in stages:
            if sid in ("understanding", "report"):
                continue
            role = templates.get(sid, {}).get("agent_role", "")
            branch = BRANCH_OF.get(role)
            if branch:
                branch_stages.setdefault(branch, []).append(sid)
        for branch, sids in branch_stages.items():
            director_id = self._BRANCH_DIRECTORS.get(branch)
            director = ORG_AGENTS.get(director_id) if director_id else None
            if director is None:
                continue
            try:
                model_def = await self.svc.model_registry.get("deepseek-pro")
                model_id = "deepseek-pro"
                if model_def is None or not model_def.enabled:
                    model_id, _reason = await self.svc.router.route(
                        director, None, description=goal)
                    model_def = await self.svc.model_registry.get(model_id) if model_id else None
                provider = self.svc.providers.get(model_def.provider) if model_def else None
                if provider is None:
                    continue
                listing = "; ".join(
                    f"{sid} ({templates.get(sid, {}).get('name', sid)})"
                    for sid in sids)
                system = (
                    "You are a department director in an AI agency. The executive "
                    "planned these stages for YOUR team for one goal. Keep only the "
                    "stages genuinely needed for this goal — your agents must not do "
                    "busywork like reviewing or testing things that were never built. "
                    "Reply with a comma-separated list of the stage ids you keep, or "
                    "the single word NONE.")
                req = ModelRequest(model_id=model_id, system=system,
                                   messages=[{"role": "user", "content":
                                              f"Goal: {goal}\nYour team's planned "
                                              f"stages: {listing}"}],
                                   agent_id=director.id, temperature=0.1,
                                   max_tokens=300)
                resp = await provider.complete(req)
                text = (resp.content or "").strip().lower()
                tokens = set(_re.split(r"[^a-z0-9_-]+", text))
                matched = {sid for sid in sids if sid in tokens}
                if "none" in tokens:
                    for sid in sids:
                        kept.remove(sid)
                elif matched:
                    for sid in sids:
                        if sid not in matched:
                            kept.remove(sid)
                # unparseable answer: keep the planned stages (fail open)
            except Exception as exc:  # noqa: BLE001
                logger.warning("director gate failed for branch %s: %s — keeping stages",
                               branch, exc)
        return kept

    def _prior_stage_context(self, run_id: str, stage_id: str) -> str:
        """Prior stage outputs, so a stage agent never starts from nothing."""
        run = self._active_runs.get(run_id)
        if not run:
            return ""
        results = run.get("state", {}).get("stage_results", {})
        parts = []
        for sid, res in results.items():
            if sid == stage_id or sid.startswith("__"):
                continue
            output = (res.get("output") or "").strip()
            if not output:
                continue
            parts.append(f"## {sid}\n{output[:3000]}")
        return "\n\n".join(parts)

    async def run_workflow(self, workflow_id: str, project_id: str,
                           entry_stage: Optional[str] = None) -> dict:
        workflow = await self.svc.workflow_registry.get(workflow_id)
        if workflow is None:
            raise KeyError(f"workflow {workflow_id} not found")
        return await self.run_workflow_obj(workflow, project_id, entry_stage)

    async def run_workflow_obj(self, workflow: WorkflowDef, project_id: str,
                               entry_stage: Optional[str] = None) -> dict:
        project = await self.svc.projects.require(project_id)
        run_id = new_id("run")
        state = new_state(run_id, workflow.workflow_id, project_id,
                          entry_stage or workflow.entry_stage,
                          [s.model_dump() for s in workflow.stages])
        self._active_runs[run_id] = {"workflow": workflow, "state": state}
        await self.svc.events.publish("workflow.started",
                                      {"workflow_id": workflow.workflow_id, "run_id": run_id},
                                      project_id=project_id, source="orchestrator")
        final = await self._invoke(run_id, state)
        return final

    async def resume(self, run_id: str, approval_decision: str, note: str = "",
                     decided_by: str = "human") -> dict:
        """Resume a paused run after a human approval decision.

        Survives restarts: if the run is not in memory, the latest durable
        checkpoint is rehydrated from the checkpointer and the workflow is
        rebuilt from its persisted id."""
        entry = self._active_runs.get(run_id)
        if entry is None:
            state = await self._checkpoint_state(run_id)
            if state is None:
                raise KeyError(f"unknown run {run_id}")
            workflow_id = state.get("workflow_id")
            workflow = await self.svc.workflow_registry.get(workflow_id) \
                if workflow_id else None
            if workflow is None:
                raise KeyError(f"run {run_id}: persisted workflow {workflow_id} not found")
            self._active_runs[run_id] = {"workflow": workflow, "state": state}
            entry = self._active_runs[run_id]
        state = dict(entry["state"])
        state["approval_decision"] = approval_decision
        state["approval_note"] = note
        state["status"] = "running"
        final = await self._invoke(run_id, state)
        return final

    async def _checkpoint_state(self, run_id: str) -> Optional[dict]:
        """Rehydrate the latest durable checkpoint for a run (restart resume)."""
        try:
            ckpt = await self.get_graph().checkpointer.aget_tuple(
                {"configurable": {"thread_id": run_id}})
        except Exception:  # noqa: BLE001
            return None
        if ckpt is None:
            return None
        values = ckpt.checkpoint.get("channel_values") or {}
        return values if isinstance(values, dict) else None

    async def checkpoint_stats(self) -> dict:
        """How many durable checkpoints exist (observability)."""
        try:
            saver = self.get_graph().checkpointer
            return {"checkpoints": await saver.checkpoint_count(),
                    "threads": len(await saver.list_threads())}
        except Exception:  # noqa: BLE001
            return {"checkpoints": 0, "threads": 0}

    async def approve(self, approval_id: str, decision: str, decided_by: str = "human",
                      note: str = "") -> Optional[dict]:
        """Human decides an approval; resume the paused workflow run if any."""
        approval = await self.svc.approvals.get(approval_id)
        if approval is None:
            return None
        if approval.status.value == "pending":
            await self.svc.approvals.decide(approval_id, decision, decided_by, note=note)
        # find a run paused on this approval
        for run_id, entry in self._active_runs.items():
            state = entry.get("state", {})
            if state.get("pending_approval_id") == approval_id and state.get("status") == "awaiting_approval":
                decision_map = {"approved": "approved", "rejected": "rejected",
                                "changes_requested": "rejected"}
                return await self.resume(run_id, decision_map.get(decision, "rejected"),
                                         note=note, decided_by=decided_by)
        return {"status": "no_paused_run", "approval_id": approval_id}

    async def run_single_task(self, task_id: str) -> AgentRunResult:
        """Execute one task (worker path). Returns the agent run result."""
        task = await self.svc.tasks.require(task_id)
        if not task.assigned_agent:
            raise ValueError(f"task {task_id} has no assigned agent")
        agent = await self.svc.agent_registry.get(task.assigned_agent)
        if not agent or not agent.enabled:
            raise ValueError(f"agent {task.assigned_agent} unavailable")
        project = await self.svc.projects.get(task.project_id)
        # drain structured messages so the agent starts with full context
        try:
            for message in await self.svc.messages.inbox(agent.id, unread_only=True, limit=10):
                await self.svc.messages.mark_delivered(message.message_id)
        except Exception:  # noqa: BLE001
            pass
        return await self._run_agent_for_task(agent, task, project)

    # ------------------------------------------------------------------
    # Stage execution
    # ------------------------------------------------------------------
    async def execute_stage_work(self, workflow: WorkflowDef, stage: WorkflowStage,
                                 project: Project, run_id: str) -> StageResult:
        """Instantiate the stage's agent and run the stage's task.

        With WORKSPACE_ISOLATION=true, coding stages execute inside their own
        git worktree cut from the project workspace; the work merges back to
        the base branch when the stage finishes (plain-dir projects are
        synced back instead). Failures never lose the worktree contents.
        """
        result = StageResult(stage_id=stage.stage_id, agent_id=stage.agent_role)
        agent = await self.svc.agent_registry.get(stage.agent_role)
        if agent is None or not agent.enabled:
            result.status = "failed"
            result.error = f"stage agent {stage.agent_role} unavailable"
            return result

        # -- isolated workspace for coding stages (opt-in) --------------------
        canonical = self.svc.workspace / project.project_id
        workspace_root = canonical
        record = None
        if getattr(self.svc.settings, "workspace_isolation", False) \
                and stage.stage_id in _ISOLATED_STAGES:
            try:
                record = await self.svc.workspaces.isolate(
                    project.project_id, task_id="", agent_id=stage.agent_role,
                    repo_path=str(canonical))
                workspace_root = Path(record.worktree_path)
            except Exception:  # noqa: BLE001
                logger.exception("workspace isolation failed for stage %s — "
                                 "running in the shared workspace", stage.stage_id)
                record = None

        description = stage.description or f"Execute workflow stage {stage.name}."
        # on retries, tell the agent its previous approach failed
        previous = await self.svc.tasks.by_project(project.project_id)
        attempts = sum(1 for t in previous
                       if t.title.startswith(f"{stage.name} (") and t.status.value == "failed")
        if attempts:
            description = (description + "\n\nRETRY NOTE: a previous attempt at this stage "
                           "failed to produce usable output. Change your approach — gather "
                           "evidence with tools FIRST, then write the complete deliverable.")
        context = self._prior_stage_context(run_id, stage.stage_id)
        if context:
            description = (description + "\n\n=== WORK COMPLETED SO FAR (read this before "
                           "touching tools) ===\n" + context)
        task = await self.svc.tasks.create(
            project.project_id, title=f"{stage.name} ({stage.stage_id})",
            description=description,
            assigned_agent=agent.id, created_by="orchestrator",
            priority="high" if stage.requires_approval else "normal",
        )
        result.task_id = task.task_id
        await self.svc.tasks.set_status(task.task_id, TaskStatus.RUNNING)
        await self.svc.agent_registry.set_status(
            agent.id, AgentStatus.WORKING, task_id=task.task_id,
            project_id=project.project_id, model=result.model or "",
            action=f"stage {stage.stage_id}")
        result.started_at = task.created_at

        tracer = getattr(self.svc, "tracer", None)
        span = None
        span_cm = None
        if tracer is not None:
            span_cm = tracer.span(
                kind="stage", name=f"stage:{stage.stage_id}", trace_id=run_id,
                agent_id=agent.id, task_id=task.task_id,
                project_id=project.project_id, payload={"workflow": workflow.workflow_id})
            span = await span_cm.__aenter__()
        run = self.svc.runtime(agent, task, project, workspace_path=workspace_root)
        outcome = await run.run(task)
        result.model = outcome.model
        result.cost = outcome.cost
        result.error = outcome.error
        result.reflection = outcome.reflection
        result.artifacts = outcome.artifacts
        result.output = outcome.content
        if span is not None and span_cm is not None:
            span.status = "ok" if not outcome.error else "error"
            span.error = outcome.error
            span.model = outcome.model
            span.cost = outcome.cost
            span.result_summary = (outcome.content or "")[:200]
            await span_cm.__aexit__(None, None, None)

        # persist the actual deliverable for the build stages: the agent's
        # final answer IS the HTML source — anything less is a failed stage
        if stage.stage_id in ("implement-frontend", "implement-backend"):
            filename = "index.html" if stage.stage_id == "implement-frontend" else "demo.html"
            import re as _re
            m = _re.search(r"(<!DOCTYPE.*|<!doctype.*|<html.*</html>)",
                           outcome.content or "", _re.I | _re.S)
            html = m.group(1) if m else ""
            if html:
                try:
                    target = workspace_root / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(html)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to persist %s", filename)
            else:
                # the model may have written the file directly instead —
                # a real deliverable on disk satisfies the contract
                on_disk = workspace_root / filename
                if on_disk.exists() and on_disk.stat().st_size > 2000:
                    result.artifacts.append(filename)
                elif result.status == "completed":
                    result.status = "failed"
                    result.error = (f"stage {stage.stage_id} completed without the "
                                    f"{filename} deliverable (answer or file)")

        # mirror the agent's full response into its branch channel so the
        # humans can read the actual work (PRDs, briefs, reports, arguments)
        mm = getattr(self.svc, "mattermost", None)
        if mm is not None and mm.available and outcome.content:
            try:
                from agentos.agents.personas import (branch_channel_for,
                                                     token_for_agent, username_for)
                label = f"**[{stage.stage_id.upper()} — {stage.name}]**\n"
                body = outcome.content
                if len(body) > 16000:
                    body = body[:16000] + "\n…(truncated at platform limit)"
                token = token_for_agent(agent.id)
                if token:
                    await mm.post_as_user(username_for(agent.id), token,
                                          label + body,
                                          branch_channel_for(agent.id))
                else:
                    await mm.post_as_agent(agent, label + body,
                                           channel=branch_channel_for(agent.id))
                # checkpoints: the boss reads understanding + final report
                # right where they asked for the work
                home = mm._project_channels.get(project.project_id)
                if home and stage.stage_id in ("understanding", "report"):
                    if token:
                        await mm.post_as_user(username_for(agent.id), token,
                                              label + body, home)
                    else:
                        await mm.post_to(home, label + body)
            except Exception:  # noqa: BLE001
                logger.exception("stage report mirror failed")

        # guaranteed artifacts: the stage template declares its deliverable
        # (e.g. artifacts/research.md); if the model never wrote it (or left a
        # stub), the agent's actual output is persisted as the artifact so the
        # run always ships the planned files. Never overwrites real work.
        if stage.artifact_prefix:
            try:
                artifact_target = workspace_root / stage.artifact_prefix
                stub = (not artifact_target.exists()
                        or artifact_target.stat().st_size < 200)
                if stub and (outcome.content or "").strip():
                    artifact_target.parent.mkdir(parents=True, exist_ok=True)
                    artifact_target.write_text(outcome.content)
                    name = stage.artifact_prefix.split("/")[-1]
                    if name not in outcome.artifacts:
                        outcome.artifacts.append(name)
            except Exception:  # noqa: BLE001
                logger.exception("guaranteed artifact write failed for %s",
                                 stage.artifact_prefix)
        # persist artifacts against the task + project
        for artifact in outcome.artifacts:
            await self.svc.tasks.record_artifact(task.task_id, artifact)
            await self.svc.projects.add_artifact(project.project_id, artifact)
        await self._record_usage(task, outcome)

        # -- merge the isolated workspace back (never lose the work) ----------
        if record is not None:
            try:
                merge = await self.svc.workspaces.integrate(record.workspace_id,
                                                            operator=agent.id)
                if record.mode == "plain":
                    # plain sandboxes have no git merge: sync the deliverables
                    # into the canonical project workspace so downstream stages
                    # and the deploy tooling still see them
                    import shutil

                    if Path(record.worktree_path).exists():
                        shutil.copytree(record.worktree_path, canonical,
                                        dirs_exist_ok=True)
                await self.svc.events.publish(
                    "workspace.merged",
                    {"workspace": record.workspace_id, "stage": stage.stage_id,
                     "mode": record.mode, "merged": merge.get("merged", False),
                     "commits": merge.get("commits", []),
                     "note": merge.get("note", "")},
                    project_id=project.project_id, task_id=task.task_id,
                    agent_id=agent.id)
            except Exception:  # noqa: BLE001
                logger.exception("workspace merge failed for %s", record.workspace_id)

        # every stage outcome gets evaluated (deterministic) + recorded
        try:
            record = await self.svc.evaluation.deterministic.evaluate(task, outcome)
            record.workflow_id = workflow.workflow_id
            record.skill_id = stage.stage_id
            await self.svc.entity_store.save("evaluation", record)
            await self.svc.performance.record_evaluation(record)
        except Exception:  # noqa: BLE001
            pass

        # structured handoff to the agent's parent (multi-agent visibility)
        try:
            if agent.parent_agent:
                await self.svc.messages.send_handoff(
                    agent.id, agent.parent_agent,
                    artifacts=outcome.artifacts,
                    note=f"stage {stage.stage_id} finished: "
                         f"{'ok' if not outcome.error else 'failed'}",
                    task_id=task.task_id, project_id=project.project_id)
        except Exception:  # noqa: BLE001
            pass

        if outcome.error and outcome.fail_class not in TRANSIENT_CLASSES:
            result.status = "failed"
            await self.svc.tasks.set_status(task.task_id, TaskStatus.FAILED, error=outcome.error)
            await self.svc.agent_registry.set_status(agent.id, AgentStatus.FAILED,
                                                     task_id=task.task_id,
                                                     project_id=project.project_id,
                                                     error=outcome.error)
            return result

        if outcome.error and outcome.fail_class in TRANSIENT_CLASSES:
            result.status = "failed"
            result.error = outcome.error
            await self.svc.tasks.set_status(task.task_id, TaskStatus.FAILED, error=outcome.error)
            await self.svc.agent_registry.set_status(agent.id, AgentStatus.BLOCKED,
                                                     task_id=task.task_id,
                                                     error=outcome.error)
            return result

        if result.error:
            # contract enforcement already marked this stage failed
            # (e.g. a build stage without its deliverable) — never let the
            # tail clobber that back to "completed"
            result.status = "failed"
            await self.svc.tasks.set_status(task.task_id, TaskStatus.FAILED,
                                            error=result.error)
            await self.svc.agent_registry.set_status(
                agent.id, AgentStatus.FAILED, task_id=task.task_id,
                project_id=project.project_id, error=result.error)
            return result

        result.status = "completed"
        await self.svc.tasks.set_status(task.task_id, TaskStatus.COMPLETED,
                                        result=outcome.content[:4000])
        await self.svc.agent_registry.set_status(agent.id, AgentStatus.IDLE)
        await self.svc.events.publish("stage.completed", {"stage": stage.stage_id},
                                      project_id=project.project_id, task_id=task.task_id,
                                      agent_id=agent.id)
        return result

    # ------------------------------------------------------------------
    # Agent spawning (delegation)
    # ------------------------------------------------------------------
    async def spawn_subagent(self, parent_task: Task, child_agent_id: str,
                             description: str, depth: int = 0) -> AgentRunResult:
        """Run a child agent for a delegated subtask, with hard limits and
        strategy-change recovery: a transient failure is retried once with a
        stronger model before escalating to the parent."""
        settings = self.svc.settings
        if depth >= settings.max_agent_depth:
            raise SpawnLimitError(
                f"agent depth limit reached ({settings.max_agent_depth}) for {child_agent_id}")
        if self._parallel_count >= settings.max_parallel_agents:
            raise SpawnLimitError(f"parallel agent limit reached ({settings.max_parallel_agents})")
        hash_key = f"{parent_task.task_id}:{child_agent_id}:{description[:80]}"
        if hash_key in self._active_task_hashes:
            raise SpawnLimitError(f"duplicate active task detected: {child_agent_id} for {description[:60]}")

        agent = await self.svc.agent_registry.get(child_agent_id)
        if agent is None or not agent.enabled:
            raise SpawnLimitError(f"child agent {child_agent_id} unavailable")

        child = await self.svc.tasks.create(
            parent_task.project_id, title=f"{agent.name}: {description[:100]}",
            description=description, assigned_agent=child_agent_id,
            parent_task=parent_task.task_id, created_by=parent_task.assigned_agent or "executive",
        )
        self._active_task_hashes[hash_key] = child.task_id
        self._parallel_count += 1
        forced = None
        try:
            project = await self.svc.projects.get(parent_task.project_id)
            result = await self._run_agent_for_task(agent, child, project, depth=depth)
            # recoverable failure → retry once with a strategy change (stronger
            # model for this run only), then escalate to the parent.
            if result.error and result.fail_class in TRANSIENT_CLASSES and depth <= settings.max_agent_depth:
                await self.svc.events.publish(
                    "agent.retry_strategy_changed",
                    {"agent": agent.id, "task": child.task_id,
                     "reason": f"{result.fail_class} failure — retrying with stronger model"},
                    project_id=parent_task.project_id, task_id=child.task_id,
                    agent_id=agent.id, severity="warning")
                forced = await self._stronger_model(agent)
                result = await self._run_agent_for_task(agent, child, project, depth=depth,
                                                        force_model=forced)
            if result.error:
                await self.svc.messages.send(
                    "escalation", agent.id, parent_task.assigned_agent or "executive",
                    {"task_id": child.task_id, "error": str(result.error)[:500],
                     "fail_class": result.fail_class,
                     "parent_task": parent_task.task_id,
                     "retried_with": forced},
                    project_id=parent_task.project_id, task_id=child.task_id,
                    priority="high", requires_response=True)
            # downstream-success tracking: delegation outcomes fold back into
            # the parent agent's performance record (self-improvement signal)
            try:
                if parent_task.assigned_agent:
                    await self.svc.performance.record_downstream(
                        parent_task.assigned_agent, result.error is None)
            except Exception:  # noqa: BLE001
                pass
            return result
        finally:
            self._parallel_count -= 1
            self._active_task_hashes.pop(hash_key, None)

    async def _stronger_model(self, agent: Any) -> Optional[str]:
        """Pick a model one tier above the agent's policy (strategy change)."""
        tier = agent.model_policy.get("tier", "t2")
        stronger = {"t1": "t2", "t2": "t3"}.get(tier, "t3")
        model_id = await self.svc.router.pick_for_provider(stronger)
        if model_id:
            return model_id
        return await self.svc.router.pick_for_provider("t3") or await self.svc.router.pick_for_provider("t2")

    async def pick_child(self, parent: Any, description: str) -> Optional[str]:
        """Performance-aware delegation: among a parent's enabled children,
        prefer the one with the best track record for this kind of work."""
        children = [a for a in await self.svc.agent_registry.list(enabled_only=True)
                    if a.parent_agent == parent.id]
        if not children:
            return None
        ranked = []
        for child in children:
            stats = await self.svc.performance.stats(child.id, "all")
            ranked.append((child, stats))
        ranked.sort(key=lambda pair: (pair[1].success_rate, -pair[1].avg_cost,
                                      pair[1].runs), reverse=True)
        best, stats = ranked[0]
        if stats.runs >= self.svc.settings.perf_min_runs_for_influence:
            return best.id
        return children[0].id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _run_agent_for_task(self, agent: Any, task: Task, project: Optional[Project],
                                  depth: int = 0,
                                  force_model: Optional[str] = None) -> AgentRunResult:
        await self.svc.tasks.set_status(task.task_id, TaskStatus.RUNNING)
        await self.svc.agent_registry.set_status(
            agent.id, AgentStatus.WORKING, task_id=task.task_id,
            project_id=task.project_id, action=f"depth {depth}")
        run = self.svc.runtime(agent, task, project, spawn_depth=depth)
        outcome = await run.run(task, force_model=force_model)
        task = await self.svc.tasks.get(task.task_id)  # refresh
        if task:
            task.model_used = outcome.model
            task.cost = outcome.cost
            for artifact in outcome.artifacts:
                if artifact not in task.artifacts:
                    task.artifacts.append(artifact)
            if outcome.error:
                task.error = outcome.error
                task.status = TaskStatus.FAILED
                await self.svc.agent_registry.set_status(agent.id, AgentStatus.FAILED,
                                                         task_id=task.task_id, error=outcome.error)
            else:
                task.status = TaskStatus.COMPLETED
                task.result = outcome.content[:4000]
                await self.svc.agent_registry.set_status(agent.id, AgentStatus.IDLE)
            await self.svc.tasks.save(task)
            await self._record_usage(task, outcome)
        await self.svc.events.publish(
            "task.completed" if not outcome.error else "task.failed",
            {"task": task.title if task else "", "agent": agent.id},
            project_id=task.project_id, task_id=task.task_id, agent_id=agent.id,
            severity="error" if outcome.error else "info")
        return outcome

    async def _record_usage(self, task: Task, outcome: AgentRunResult) -> None:
        for usage in outcome.usage:
            await self.svc.budgets.record(usage)
            await self.svc.entity_store.save("usage", usage)
        if not outcome.model:
            return
        await self.svc.events.publish(
            "model.completed",
            {"model": outcome.model, "cost": outcome.cost,
             "calls": len(outcome.usage)},
            project_id=task.project_id, task_id=task.task_id,
            agent_id=task.assigned_agent)

    # -- graph invocation ---------------------------------------------------
    async def _invoke(self, run_id: str, state: dict) -> dict:
        graph = self.get_graph()
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 200}
        try:
            final = await graph.ainvoke(state, config=config)
        except Exception as exc:  # noqa: BLE001
            logger.exception("workflow run %s crashed", run_id)
            state["status"] = "failed"
            state["error"] = f"workflow crash: {exc}"
            final = state
        self._active_runs[run_id]["state"] = final
        return final

    def get_graph(self):
        if self._graph is None:
            from agentos.orchestration.graph import build_graph

            self._graph = build_graph(self)
        return self._graph

    # -- worker loop --------------------------------------------------------
    async def worker_loop(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """Consume the task queue and run tasks (with graceful shutdown).

        Concurrency guard (§34): before executing a task the worker takes a
        distributed lock (`task:<id>:exec`) so two workers on different
        processes/containers can never run the same task simultaneously. The
        DB-status check below is a second, cheaper guard; the lock is the
        arbiter across processes. A task left enqueued after its owner dies
        (lock TTL expiry) is simply picked up by the next worker.
        """
        logger.info("worker started")
        owner = f"worker:{os.getpid()}"
        while True:
            if stop_event and stop_event.is_set():
                break
            item = await self.svc.queue.dequeue(timeout=1.0)
            if item is None:
                continue
            task_id = item.get("task_id")
            if not task_id:
                continue
            # duplicate/parallel-execution guard: a task that is already being
            # worked, already queued for work, or already finished must never
            # run again (worker loops can resurrect queued tasks otherwise)
            task = await self.svc.tasks.get(task_id)
            if task and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING,
                                        TaskStatus.COMPLETED):
                logger.info("skipping duplicate queued task %s (status %s)",
                            task_id, task.status.value)
                continue
            # distributed mutual exclusion: another worker owns it? skip.
            lock_key = f"task:{task_id}:exec"
            if not await self.svc.locks.acquire(lock_key, owner=owner):
                logger.info("task %s already locked by another worker — skipping",
                            task_id)
                continue
            try:
                await self.run_single_task(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("task %s failed in worker", task_id)
                task = await self.svc.tasks.get(task_id)
                if task:
                    await self.svc.tasks.set_status(task_id, TaskStatus.FAILED,
                                                    error=str(exc))
            finally:
                await self.svc.locks.release(lock_key, owner)
        logger.info("worker stopped")


def _slug(text: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9\s-]", "", text.lower()).strip()
    return re.sub(r"\s+", "-", slug)[:60] or "project"