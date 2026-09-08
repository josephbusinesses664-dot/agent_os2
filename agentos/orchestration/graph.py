"""LangGraph workflow state machine.

Nodes:
  step  — executes one stage (or applies a human approval decision), then
          decides the next action: continue → retry → approval (pause) →
          failed → complete.

The graph is checkpointed per run_id, so a paused (awaiting_approval) or
interrupted run can be resumed from its saved state. Conditional routing
keeps the pipeline modular: a workflow is just a list of stages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from agentos.domain.models import ApprovalStatus, ProjectStatus, TaskStatus, WorkflowStage
from agentos.orchestration.state import WorkflowState

logger = logging.getLogger("agentos.graph")


_RESEARCH_STAGES = {"research", "community", "requirements"}
_BUILD_STAGES = {"design", "implement-frontend", "implement-backend",
                 "testing", "security", "review", "deploy"}


def _gate_due(stage_id: str, next_stage: str, state: dict) -> bool:
    return (stage_id in _RESEARCH_STAGES and next_stage in _BUILD_STAGES
            and not state.get("exec_gate"))


def _find_stage_id(stages: list[dict], wanted: str) -> Optional[str]:
    for s in stages:
        if s.get("stage_id") == wanted:
            return wanted
    return None


async def _executive_gate(engine: Any, state: dict, project: Any) -> tuple[str, str]:
    """Alex reviews the research phase and decides GO (build) or NO-GO (report).

    The department heads bring the evidence; the executive makes the call —
    the human is not asked. Fail-closed: if the model is unreachable OR the
    research produced no evidence (tooling failed), the executive does NOT
    commit the organization to building — it stops before build and the
    failure is surfaced instead of being papered over."""
    from agentos.agents.hierarchy import ORG_AGENTS
    from agentos.domain.models import ModelRequest

    svc = engine.svc
    evidence = []
    for sid in ("understanding", "research", "community", "requirements"):
        r = state.get("stage_results", {}).get(sid)
        if r and r.get("output"):
            evidence.append(f"### {sid}\n{r['output'][:2500]}")
    body = "\n\n".join(evidence) or "(no stage outputs)"
    goal = getattr(project, "objective", "") or ""
    executive = ORG_AGENTS.get("executive")
    # prefer deepseek-pro; if it is registered but its provider is not
    # actually configured (no keys), fall back through the router instead of
    # silently running the gate with nothing — the gate must never default to
    # GO merely because tooling was missing.
    model_def = await svc.model_registry.get("deepseek-pro")
    model_id = "deepseek-pro"
    provider = None
    if model_def is not None and model_def.enabled:
        provider = svc.providers.get(model_def.provider)
    if provider is None and executive is not None:
        model_id, _reason = await svc.router.route(executive, None, description=goal)
        model_def = await svc.model_registry.get(model_id) if model_id else None
        provider = svc.providers.get(model_def.provider) if model_def else None
    verdict, rationale = "go", ""
    # fail-closed: no research evidence (tooling failed or stages produced
    # nothing) means the executive cannot honestly commit to building
    if not evidence:
        verdict, rationale = "nogo", ("research produced no evidence (tooling or "
                                       "stage failures) — not committing to build "
                                       "without it")
        await svc.events.publish(
            "executive.gate_fail_closed",
            {"reason": "no research evidence — gate defaulted to NO-GO"},
            project_id=project.project_id, source="executive", severity="warning")
        return verdict, rationale
    if provider is not None:
        try:
            req = ModelRequest(
                model_id=model_id,
                system=("You are Alex, the Executive Director of an AI agency. Your "
                        "department heads have finished the research phase of a project "
                        "and brought you the evidence. Review it and decide whether to "
                        "commit the whole organization to BUILDING the product. Reply "
                        "with exactly GO or NO-GO on the first line, then one sentence "
                        "of rationale."),
                messages=[{"role": "user", "content":
                           f"Goal: {goal}\n\nResearch evidence:\n{body}"}],
                agent_id="executive", temperature=0.2, max_tokens=200)
            resp = await provider.complete(req)
            text = (resp.content or "").strip()
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            first = lines[0].upper() if lines else ""
            if first.startswith("GO"):
                verdict = "go"
            elif first.startswith("NO"):
                verdict = "nogo"
            if len(lines) > 1:
                rationale = " ".join(lines[1:])
        except Exception as exc:  # noqa: BLE001
            # fail-closed: an unreachable executive is not a reason to build
            verdict, rationale = "nogo", (f"executive gate call failed ({exc}) — "
                                           "not committing to build on a failure")
            logger.warning("executive gate call failed: %s — defaulting to NO-GO", exc)
            await svc.events.publish(
                "executive.gate_fail_closed",
                {"reason": f"executive model unreachable: {exc}"},
                project_id=project.project_id, source="executive", severity="warning")
    mm = getattr(svc, "mattermost", None)
    if mm is not None and mm.available:
        line = ("🧭 **Alex's decision after research: "
                f"{'GO — committing to build.' if verdict == 'go' else 'NO-GO — stopping before build.'}"
                + (f" {rationale}" if rationale else ""))
        try:
            await mm.post_to("executive", line)
            home = mm._project_channels.get(state.get("project_id", ""))
            if home and home != "executive":
                await mm.post_to(home, line)
        except Exception:  # noqa: BLE001
            logger.exception("executive gate post failed")
    return verdict, rationale


def _stage_by_id(state: dict, stage_id: str) -> dict:
    for s in state["stages"]:
        if s["stage_id"] == stage_id:
            return s
    raise KeyError(f"stage {stage_id} not found in workflow")


async def step(engine: Any, state: dict) -> dict:
    """Execute one stage, or apply an approval decision on resume."""
    svc = engine.svc
    state["total_steps"] = state.get("total_steps", 0) + 1
    stage_id = state["current_stage"]
    stage = _stage_by_id(state, stage_id)
    workflow = engine._active_runs[state["run_id"]]["workflow"]
    project = await svc.projects.require(state["project_id"])
    run_id = state["run_id"]

    # -- resume path: apply a human decision on a paused stage --------------
    if state.get("approval_decision"):
        decision = state["approval_decision"]
        state["approval_decision"] = ""
        result = state["stage_results"].get(stage_id, {})
        if decision == ApprovalStatus.APPROVED.value:
            result["status"] = "completed"
            result["approved_by"] = state.get("approval_note", "")
            await svc.events.publish("approval.granted", {"stage": stage_id, "run": run_id},
                                     project_id=project.project_id, severity="info")
        else:
            result["status"] = "failed"
            result["error"] = f"stage rejected by human ({decision})"
            await svc.events.publish("approval.rejected", {"stage": stage_id, "run": run_id},
                                     project_id=project.project_id, severity="warning")
            state["stage_results"][stage_id] = result
            state["status"] = "failed"
            state["error"] = result["error"]
            await svc.projects.set_status(project.project_id, ProjectStatus.IN_REVIEW)
            return {"stage_results": state["stage_results"], "approval_decision": "",
                    "action": "failed", "status": "failed", "error": state["error"],
                    "pending_approval_id": ""}
        state["stage_results"][stage_id] = result
        task = await _task_for_stage(svc, project, stage, result)
        if task:
            await svc.tasks.set_status(task.task_id, TaskStatus.COMPLETED,
                                       result=result.get("output", "")[:4000])
        state["pending_approval_id"] = ""
        next_stage = stage.get("next")
        if next_stage:
            state["current_stage"] = next_stage
            return {"stage_results": state["stage_results"], "approval_decision": "",
                    "action": "continue", "current_stage": next_stage,
                    "pending_approval_id": ""}
        state["status"] = "completed"
        await svc.projects.set_status(project.project_id, ProjectStatus.COMPLETED)
        await svc.events.publish("workflow.completed", {"workflow": state["workflow_id"], "run": run_id},
                                 project_id=project.project_id, source="orchestrator")
        return {"stage_results": state["stage_results"], "approval_decision": "",
                "action": "complete", "status": "completed", "pending_approval_id": ""}

    # -- fresh execution path ----------------------------------------------
    await svc.projects.set_status(project.project_id, ProjectStatus.ACTIVE, stage=stage_id)
    try:
        result = await engine.execute_stage_work(workflow, WorkflowStage(**stage), project, run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("stage %s crashed", stage_id)
        result = type("R", (), {"status": "failed", "error": f"stage crash: {exc}",
                                "artifacts": [], "output": "", "model": None,
                                "cost": 0.0, "reflection": {}})()
        result.stage_id = stage_id
        result.agent_id = stage["agent_role"]
        result.task_id = None
    results = dict(state.get("stage_results", {}))
    results[stage_id] = {
        "stage_id": stage_id,
        "agent_id": stage["agent_role"],
        "task_id": result.task_id if hasattr(result, "task_id") else None,
        "status": result.status,
        "output": getattr(result, "output", "")[:4000],
        "artifacts": getattr(result, "artifacts", []),
        "model": getattr(result, "model", None),
        "cost": getattr(result, "cost", 0.0),
        "error": getattr(result, "error", None),
        "reflection": getattr(result, "reflection", {}),
    }

    if result.status == "failed":
        state["retry_count"] = state.get("retry_count", 0) + 1
        max_retries = svc.settings.max_task_retries
        if state["retry_count"] <= max_retries:
            backoff = min(2 ** state["retry_count"], 10)
            await svc.events.publish(
                "stage.retry", {"stage": stage_id, "attempt": state["retry_count"],
                                "error": getattr(result, "error", "")[:200]},
                project_id=project.project_id, source="orchestrator", severity="warning")
            await asyncio.sleep(backoff)  # transient backoff before retry
            return {"stage_results": results, "action": "retry", "retry_count": state["retry_count"]}
        state["status"] = "failed"
        state["error"] = getattr(result, "error", "stage failed")
        await svc.events.publish("workflow.failed", {"stage": stage_id, "error": state["error"]},
                                 project_id=project.project_id, source="orchestrator",
                                 severity="error")
        await svc.projects.set_status(project.project_id, ProjectStatus.IN_REVIEW)
        return {"stage_results": results, "action": "failed", "status": "failed",
                "error": state["error"]}

    # stage work completed
    if stage.get("requires_approval"):
        approval = await svc.approvals.request(
            stage["agent_role"], stage.get("name", stage_id),
            f"stage:{stage_id}",
            task_id=results[stage_id].get("task_id"),
            project_id=project.project_id,
            risk_level="high",
            reason=stage.get("description", "") or f"Workflow stage {stage_id} requires approval.",
        )
        state["pending_approval_id"] = approval.approval_id
        state["status"] = "awaiting_approval"
        await svc.events.publish("approval.requested", {"stage": stage_id, "run": run_id,
                                                        "approval_id": approval.approval_id},
                                 project_id=project.project_id, severity="warning")
        return {"stage_results": results, "action": "approval", "status": "awaiting_approval",
                "pending_approval_id": approval.approval_id}

    next_stage = stage.get("next")
    if next_stage:
        if _gate_due(stage_id, next_stage, state):
            # the gate reviews the research phase — it must SEE the stage
            # that just finished, or it decides on stale/empty evidence
            state["stage_results"] = results
            verdict, rationale = await _executive_gate(engine, state, project)
            state["exec_gate"] = {"verdict": verdict, "rationale": rationale}
            if verdict == "nogo":
                # skip the build phase entirely; go straight to the report
                next_stage = _find_stage_id(state["stages"], "report") or next_stage
        state["current_stage"] = next_stage
        return {"stage_results": results, "action": "continue", "current_stage": next_stage,
                "exec_gate": state.get("exec_gate", {})}
    state["status"] = "completed"
    await svc.projects.set_status(project.project_id, ProjectStatus.COMPLETED)
    await svc.events.publish("workflow.completed", {"workflow": state["workflow_id"], "run": run_id},
                             project_id=project.project_id, source="orchestrator")
    return {"stage_results": results, "action": "complete", "status": "completed",
            "exec_gate": state.get("exec_gate", {})}


async def _task_for_stage(svc: Any, project: Any, stage: dict, result: dict):
    tasks = await svc.tasks.by_project(project.project_id)
    for task in tasks:
        if task.title.startswith(f"{stage.get('name')} ("):
            return task
    return None


def build_graph(engine: Any) -> Any:
    graph = StateGraph(WorkflowState)

    async def step_node(state: dict) -> dict:
        return await step(engine, state)

    graph.add_node("step", step_node)

    def route(state: dict) -> str:
        action = state.get("action", "continue")
        return {"continue": "step", "retry": "step", "approval": END,
                "failed": END, "complete": END}.get(action, END)

    graph.add_edge(START, "step")
    graph.add_conditional_edges("step", route)
    # durable checkpoints: paused/interrupted runs survive restarts (the
    # entity store backs them; Postgres in production, memory in tests)
    from agentos.orchestration.checkpoints import DurableCheckpointer

    compiled = graph.compile(checkpointer=DurableCheckpointer(engine.svc.entity_store))
    return compiled