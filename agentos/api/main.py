"""Control-plane REST API and admin UI.

Services resolve through a module-level holder so the same route definitions
work for the CLI server (services passed directly) and the uvicorn entry
point (services built in lifespan and stashed in the holder).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentos.domain.models import AgentDef, McpServer, TaskStatus
from agentos.observability.health import HealthChecker

svc_holder: dict[str, Any] = {"svc": None}


class ApprovalDecision(BaseModel):
    decision: str  # approved | rejected | changes_requested
    decided_by: str = "human"
    note: str = ""


class GoalRequest(BaseModel):
    goal: str
    workflow_id: str = "zero_to_hundred"
    project_name: Optional[str] = None


class TaskCreate(BaseModel):
    project_id: str
    title: str
    description: str = ""
    assigned_agent: Optional[str] = None
    dependencies: list[str] = []
    priority: str = "normal"


class AgentCreate(BaseModel):
    id: str
    name: str
    role: str
    description: str = ""
    parent_agent: Optional[str] = None


class McpCredentials(BaseModel):
    token: str


class EmergencyEngage(BaseModel):
    operator: str = "human"
    reason: str = ""


class WorkspaceCreate(BaseModel):
    project_id: str
    task_id: str = ""
    agent_id: str = ""
    repo_path: Optional[str] = None


class IntegrateRequest(BaseModel):
    message: str = ""
    operator: str = "human"


def create_app(svc: Any) -> FastAPI:
    if svc is not None:
        svc_holder["svc"] = svc

    def S() -> Any:
        if svc_holder["svc"] is None:
            raise RuntimeError("services not initialized (start the control plane)")
        return svc_holder["svc"]

    app = FastAPI(title="Agent OS Control Plane", version="0.1.0")

    # -- API auth gate (opt-in) ----------------------------------------------
    # When API_AUTH_TOKEN is set, every /api/* route except /api/health
    # requires `Authorization: Bearer <token>`. The admin UI and static assets
    # stay open (they render inside the trusted network); the control plane
    # protects the data.
    @app.middleware("http")
    async def api_auth_gate(request: Request, call_next):
        svc = svc_holder.get("svc")
        token = getattr(getattr(svc, "settings", None), "api_auth_token", None) \
            if svc is not None else None
        path = request.url.path
        if token and path.startswith("/api/") and path != "/api/health":
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {token}":
                return JSONResponse({"detail": "unauthorized — Bearer token required"},
                                    status_code=401)
        return await call_next(request)

    # -- health -------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        reports = await HealthChecker(S()).check_all()
        return {"status": "ok", "services": [r.model_dump() for r in reports],
                "overall": HealthChecker(S()).overall(reports)}

    @app.get("/api/status")
    async def status():
        svc = S()
        agents = await svc.agent_registry.list(enabled_only=True)
        instances = await svc.agent_registry.list_instances()
        budgets = await svc.budgets.summary()
        return {
            "agents": {
                "available": len(agents),
                "active": sum(1 for i in instances if i.status.value in
                              ("working", "awaiting_approval", "awaiting_review", "blocked")),
                "instances": [i.model_dump(mode="json") for i in instances],
            },
            "skills": len(await svc.skill_registry.list()),
            "tools": len(await svc.tool_registry.list()),
            "models": len(await svc.model_registry.list()),
            "mcp": len(await svc.mcp_registry.list()),
            "apis": len(await svc.api_registry.list()),
            "workflows": len(await svc.workflow_registry.list()),
            "projects": len(await svc.projects.list()),
            "tasks": len(await svc.tasks.list()),
            "approvals_pending": len(await svc.approvals.pending()),
            "budget_spent_month": round(sum(b.get("spent_month", 0) for b in budgets), 4),
            "mattermost": bool(svc.mattermost and svc.mattermost.available),
        }

    # -- agents -------------------------------------------------------------
    @app.get("/api/agents")
    async def list_agents(enabled: bool = True):
        return [a.model_dump(mode="json") for a in
                await S().agent_registry.list(enabled_only=enabled)]

    @app.post("/api/agents")
    async def create_agent(body: AgentCreate):
        agent = AgentDef(id=body.id, name=body.name, role=body.role,
                         description=body.description, parent_agent=body.parent_agent)
        await S().agent_registry.create(agent)
        return agent.model_dump(mode="json")

    @app.patch("/api/agents/{agent_id}/enabled")
    async def set_agent_enabled(agent_id: str, enabled: bool):
        try:
            agent = await S().agent_registry.disable(agent_id, enabled=enabled)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return agent.model_dump(mode="json")

    @app.get("/api/agent-instances")
    async def agent_instances():
        return [i.model_dump(mode="json") for i in await S().agent_registry.list_instances()]

    # -- tasks --------------------------------------------------------------
    @app.get("/api/tasks")
    async def list_tasks(project_id: Optional[str] = None, status: Optional[str] = None,
                         limit: int = 200):
        status_enum = TaskStatus(status) if status else None
        return [t.model_dump(mode="json") for t in
                await S().tasks.list(status=status_enum, project_id=project_id, limit=limit)]

    @app.post("/api/tasks")
    async def create_task(body: TaskCreate):
        task = await S().tasks.create(body.project_id, body.title, body.description,
                                      assigned_agent=body.assigned_agent,
                                      dependencies=body.dependencies, priority=body.priority)
        return task.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str):
        await S().queue.enqueue({"task_id": task_id})
        return {"queued": task_id}

    # -- projects -----------------------------------------------------------
    @app.get("/api/projects")
    async def list_projects():
        return [p.model_dump(mode="json") for p in await S().projects.list()]

    @app.post("/api/projects")
    async def create_project(name: str, objective: str = ""):
        project = await S().projects.create(name, objective)
        return project.model_dump(mode="json")

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        svc = S()
        project = await svc.projects.get(project_id)
        if not project:
            raise HTTPException(404, "project not found")
        tasks = [t.model_dump(mode="json") for t in await svc.tasks.by_project(project_id)]
        return {"project": project.model_dump(mode="json"), "tasks": tasks}

    # -- goals / workflows --------------------------------------------------
    @app.post("/api/goals")
    async def execute_goal(body: GoalRequest):
        try:
            return await S().engine.execute_goal(body.goal, user_id="api",
                                                 workflow_id=body.workflow_id,
                                                 project_name=body.project_name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/workflows")
    async def list_workflows():
        return [w.model_dump(mode="json") for w in await S().workflow_registry.list()]

    # -- skills / tools / mcp / apis / models -------------------------------
    @app.get("/api/skills")
    async def list_skills(category: Optional[str] = None):
        return [s.model_dump(mode="json") for s in
                await S().skill_registry.list(category=category)]

    @app.get("/api/tools")
    async def list_tools():
        return [t.model_dump(mode="json") for t in await S().tool_registry.list()]

    @app.get("/api/mcp")
    async def list_mcp():
        return await S().mcp_registry.list_public()  # credentials redacted

    @app.post("/api/mcp")
    async def register_mcp(server: McpServer):
        await S().mcp_registry.register(server)
        return server.public_dict()

    @app.post("/api/mcp/{name}/credentials")
    async def set_mcp_credentials(name: str, body: McpCredentials):
        await S().mcp_registry.set_credentials(name, body.token)
        return {"server": name, "credentials": "set"}

    # -- memory graph -------------------------------------------------------
    @app.get("/api/memory/graph")
    async def memory_graph(scope: str = "project", owner_id: str = ""):
        if not owner_id:
            return {"error": "owner_id required"}
        return await S().memory.graph(scope, owner_id)

    @app.get("/api/apis")
    async def list_apis():
        svc = S()
        return [{"api": a.model_dump(mode="json"), "evaluation": svc.api_registry.evaluate(a)}
                for a in await svc.api_registry.list()]

    @app.get("/api/models")
    async def list_models():
        return [m.model_dump(mode="json") for m in await S().model_registry.list()]

    # -- budget / usage -----------------------------------------------------
    @app.get("/api/budget")
    async def budget():
        return {"budgets": await S().budgets.summary()}

    @app.get("/api/usage")
    async def usage(limit: int = 200):
        records = await S().entity_store.list_docs("usage")
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:limit]

    # -- tracing ------------------------------------------------------------
    @app.get("/api/traces/{task_id}")
    async def task_trace(task_id: str):
        return await S().tracer.task_trace(task_id)

    @app.get("/api/traces")
    async def traces(limit: int = 200):
        return await S().tracer.recent(limit=limit)

    @app.get("/api/trace-summary")
    async def trace_summary():
        return await S().tracer.summary()

    # -- performance / evaluation -------------------------------------------
    @app.get("/api/performance")
    async def performance():
        return await S().entity_store.list_docs("performance")

    @app.get("/api/performance/{agent_id}")
    async def performance_agent(agent_id: str, window: str = "all"):
        return (await S().performance.stats(agent_id, window)).model_dump(mode="json")

    @app.get("/api/leaderboard")
    async def leaderboard(metric: str = "success_rate", limit: int = 10):
        return await S().performance.leaderboard(limit=limit, metric=metric)

    @app.get("/api/evaluation")
    async def evaluation(limit: int = 100):
        records = await S().entity_store.list_docs("evaluation")
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:limit]

    @app.post("/api/evaluation/benchmark")
    async def run_benchmark(dataset: str = "basic", agent: Optional[str] = None):
        try:
            return (await S().evaluation.run_dataset(
                dataset, agent_override=agent)).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/evaluation/datasets")
    async def eval_datasets():
        from agentos.evaluation.datasets import list_datasets

        return list_datasets(Path(S().settings.eval_sets_dir))

    @app.get("/api/evaluation/failure-analysis")
    async def failure_analysis():
        from agentos.evaluation.failure_analysis import analyze_svc

        return await analyze_svc(S())

    @app.get("/api/evaluation/compare")
    async def compare(group_by: str = "skill_id", limit: int = 10):
        from agentos.evaluation import leaderboard
        from agentos.domain.models import EvaluationRecord

        records = [EvaluationRecord.model_validate(r)
                   for r in await S().entity_store.list_docs("evaluation")]
        return leaderboard(records, group_by=group_by, limit=limit)

    # -- dynamic planning ---------------------------------------------------
    @app.post("/api/plan")
    async def plan_goal(goal: str):
        return S().planner.plan_summary(goal)

    @app.post("/api/plan/execute")
    async def execute_planned(goal: str, expand_full: bool = False):
        return await S().engine.execute_dynamic(goal, user_id="api",
                                                expand_full=expand_full)

    # -- tool / mcp health --------------------------------------------------
    @app.get("/api/tools/health")
    async def tool_health(tool: Optional[str] = None):
        return await S().tool_registry.health(tool or "")

    @app.get("/api/mcp/health")
    async def mcp_health():
        svc = S()
        reports = []
        for server in await svc.mcp_registry.list():
            reports.append(await svc.mcp_registry.health_check(server.name))
        ok = sum(1 for r in reports if r.get("status") == "ok")
        return {"status": "ok" if ok == len(reports) else "degraded",
                "healthy": ok, "total": len(reports), "servers": reports}

    # -- events / audit -----------------------------------------------------
    @app.get("/api/events")
    async def events(limit: int = 100, event_type: Optional[str] = None):
        return [e.to_dict() for e in
                await S().events.recent(limit=limit, event_type=event_type)]

    @app.get("/api/audit")
    async def audit(limit: int = 100):
        return [e.model_dump(mode="json") for e in await S().audit.query(limit=limit)]

    # -- approvals ----------------------------------------------------------
    @app.get("/api/approvals")
    async def approvals(pending_only: bool = True):
        svc = S()
        if pending_only:
            return [a.model_dump(mode="json") for a in await svc.approvals.pending()]
        return [a.model_dump(mode="json") for a in await svc.approvals.all()]

    @app.post("/api/approvals/{approval_id}/decide")
    async def decide(approval_id: str, body: ApprovalDecision):
        try:
            result = await S().engine.approve(approval_id, body.decision,
                                              decided_by=body.decided_by, note=body.note)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"approval_id": approval_id, "result": result}

    # -- hard security policies / emergency stop ------------------------------
    @app.get("/api/security/policies")
    async def security_policies():
        svc = S()
        return [p.model_dump(mode="json") for p in svc.policy.policies()]

    @app.get("/api/security/emergency")
    async def emergency_status():
        return (await S().policy.emergency_status()).to_dict()

    @app.post("/api/security/emergency")
    async def emergency_engage(body: EmergencyEngage):
        return (await S().policy.engage(body.operator, body.reason)).to_dict()

    @app.delete("/api/security/emergency")
    async def emergency_disengage(operator: str = "human"):
        return (await S().policy.disengage(operator)).to_dict()

    # -- isolated engineering workspaces --------------------------------------
    @app.get("/api/workspaces")
    async def list_workspaces():
        return [w.model_dump(mode="json") for w in await S().workspaces.list()]

    @app.post("/api/workspaces")
    async def create_workspace(body: WorkspaceCreate):
        record = await S().workspaces.isolate(
            body.project_id, task_id=body.task_id, agent_id=body.agent_id,
            repo_path=body.repo_path)
        return record.model_dump(mode="json")

    @app.get("/api/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str):
        record = await S().workspaces.get(workspace_id)
        if not record:
            raise HTTPException(404, "workspace not found")
        return record.model_dump(mode="json")

    @app.get("/api/workspaces/{workspace_id}/status")
    async def workspace_status(workspace_id: str):
        try:
            return await S().workspaces.status(workspace_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/workspaces/{workspace_id}/diff")
    async def workspace_diff(workspace_id: str):
        try:
            return {"diff": await S().workspaces.diff(workspace_id)}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/workspaces/{workspace_id}/integrate")
    async def integrate_workspace(workspace_id: str, body: IntegrateRequest):
        try:
            return await S().workspaces.integrate(workspace_id, message=body.message,
                                                  operator=body.operator)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/workspaces/{workspace_id}/discard")
    async def discard_workspace(workspace_id: str, body: IntegrateRequest):
        try:
            return await S().workspaces.discard(workspace_id, operator=body.operator)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    # -- memory / messages --------------------------------------------------
    @app.get("/api/memory")
    async def memory(limit: int = 100):
        return [m.model_dump(mode="json") for m in await S().memory.all(limit=limit)]

    @app.get("/api/memory/stats")
    async def memory_stats(scope: str = "project", owner_id: str = ""):
        if not owner_id:
            return {"error": "owner_id required"}
        return await S().memory.stats(scope, owner_id)

    @app.post("/api/memory/consolidate")
    async def consolidate_memory(scope: str = "org", owner_id: str = "org"):
        return await S().memory.consolidate(scope, owner_id)

    @app.get("/api/messages")
    async def messages(limit: int = 100):
        return [m.model_dump(mode="json") for m in await S().messages.recent(limit=limit)]

    # -- control plane: org pipeline + rollback ledger ------------------------
    @app.get("/api/control/pipeline")
    async def control_pipeline():
        """The human control plane: projects (goals) with plans, tasks,
        escalations and outcomes — the org at a glance."""
        svc = S()
        projects = await svc.projects.list()
        out = []
        for p in projects:
            tasks = await svc.tasks.by_project(p.project_id)
            task_rows = [{"task_id": t.task_id, "title": t.title,
                          "status": t.status.value,
                          "agent": t.assigned_agent,
                          "cost": t.cost, "error": t.error}
                         for t in tasks]
            out.append({
                "project_id": p.project_id, "name": p.name,
                "objective": p.objective, "stage": p.stage,
                "status": p.status.value, "cost": p.cost,
                "tasks": task_rows,
                "open_tasks": sum(1 for t in tasks if t.status.value in
                                  ("pending", "queued", "running", "blocked")),
                "failed_tasks": sum(1 for t in tasks if t.status.value == "failed"),
            })
        return out

    @app.get("/api/rollback")
    async def rollback_ledger():
        svc = S()
        ledger = getattr(svc, "rollback", None)
        if ledger is None:
            return {"stats": {}, "active": []}
        return {"stats": ledger.stats(),
                "active": [{"entry_id": e.entry_id, "tool": e.tool,
                            "agent": e.agent_id, "task_id": e.task_id,
                            "reversibility": e.reversibility,
                            "status": e.status, "note": e.note,
                            "args": e.args_redacted}
                           for e in ledger.active()]}

    @app.post("/api/rollback/{entry_id}/rollback")
    async def rollback_entry(entry_id: str):
        svc = S()
        ledger = getattr(svc, "rollback", None)
        if ledger is None:
            raise HTTPException(503, "rollback ledger unavailable")
        result = await ledger.rollback(entry_id, operator="human")
        if not result.get("ok"):
            raise HTTPException(409, result.get("error", "rollback failed"))
        return result

    @app.post("/api/rollback/task/{task_id}/rollback")
    async def rollback_task_actions(task_id: str):
        svc = S()
        ledger = getattr(svc, "rollback", None)
        if ledger is None:
            raise HTTPException(503, "rollback ledger unavailable")
        result = await ledger.rollback_task(task_id, operator="human")
        if not result.get("ok"):
            raise HTTPException(409, result.get("error", "rollback failed"))
        return result

    # -- mission timeline (observability / mission replay) --------------------
    @app.get("/api/projects/{project_id}/timeline")
    async def project_timeline(project_id: str):
        """Reconstruct a mission: events + tasks + approvals + messages for a
        project, merged chronologically — a failed run is reconstructable after
        the fact."""
        svc = S()
        project = await svc.projects.get(project_id)
        if not project:
            raise HTTPException(404, "project not found")
        items: list[dict] = []
        for e in await svc.events.recent(limit=500):
            if e.project_id == project_id:
                items.append({"ts": e.ts, "kind": "event", "type": e.type,
                              "severity": e.severity, "agent_id": e.agent_id,
                              "detail": e.payload})
        for t in await svc.tasks.by_project(project_id):
            items.append({"ts": t.created_at, "kind": "task",
                          "task_id": t.task_id, "title": t.title,
                          "status": t.status.value, "agent": t.assigned_agent,
                          "detail": {"error": t.error} if t.error else {}})
        for a in await svc.approvals.all(limit=200):
            if a.project_id == project_id:
                items.append({"ts": a.created_at, "kind": "approval",
                              "approval_id": a.approval_id, "action": a.action,
                              "status": a.status.value, "agent": a.agent_id})
        for m in await svc.messages.recent(limit=300):
            if m.project_id == project_id:
                items.append({"ts": m.created_at, "kind": "message",
                              "type": m.message_type.value, "from": m.sender,
                              "to": m.recipient, "detail": m.payload})
        items.sort(key=lambda i: i["ts"])
        return {
            "project_id": project_id,
            "objective": project.objective,
            "status": project.status.value,
            "count": len(items),
            "timeline": [{**i, "ts": i["ts"].isoformat()} for i in items],
        }

    # -- admin UI -----------------------------------------------------------
    static_dir = Path(__file__).resolve().parent.parent / "admin" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def admin_ui():
        return FileResponse(str(static_dir / "index.html"))

    return app