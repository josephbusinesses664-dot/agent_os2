"""agent-os — the Agent OS administration CLI."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import typer

from agentos.config import get_settings

app = typer.Typer(help="Agent OS — the Unified AI Agency Operating System",
                  no_args_is_help=True)

PID_FILE = Path(".agent-os.pid")


def _run(coro):
    return asyncio.run(coro)


async def _load_svc():
    from agentos.bootstrap import build_app

    return await build_app()


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@app.command()
def start(daemon: bool = typer.Option(False, "--daemon", help="Run in background")):
    """Start the control plane: API + worker + Mattermost listener."""
    if daemon:
        import subprocess

        proc = subprocess.Popen(
            [sys.executable, "-m", "agentos.cli.main", "start"],
            stdout=open("agent-os.log", "a"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        typer.echo(f"agent-os started in background (pid {proc.pid}) — log: agent-os.log")
        return
    typer.echo("Starting Agent OS…")

    async def _main():
        svc = await _load_svc()
        typer.echo(f"AI AGENCY ONLINE — {svc.settings.agent_os_name}")
        from agentos.bootstrap import run_control_plane

        await run_control_plane(svc)

    try:
        _run(_main())
    except KeyboardInterrupt:
        typer.echo("\nStopped.")


@app.command()
def stop():
    """Stop a background agent-os instance."""
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            typer.echo(f"Sent stop signal to pid {pid}")
        except ProcessLookupError:
            typer.echo("Process not running.")
        PID_FILE.unlink(missing_ok=True)
    else:
        typer.echo("No background instance (start with --daemon to run one).")


@app.command()
def status():
    """Show the live system status board."""
    async def _main():
        svc = await _load_svc()
        agents = await svc.agent_registry.list(enabled_only=True)
        instances = {i.agent_id: i for i in await svc.agent_registry.list_instances()}
        active = [a for a in agents if instances.get(a.id) and instances[a.id].status.value in
                  ("working", "awaiting_approval", "awaiting_review", "blocked")]
        running = [a for a in agents if instances.get(a.id) and instances[a.id].status.value == "working"]
        skills = len(await svc.skill_registry.list())
        tools = len(await svc.tool_registry.list())
        models = len(await svc.model_registry.list())
        mcp = len(await svc.mcp_registry.list())
        projects = len(await svc.projects.list())
        tasks = len(await svc.tasks.list())
        budgets = await svc.budgets.summary()
        total_spent = sum(b.get("spent_month", 0) for b in budgets)
        mm = "ONLINE" if svc.mattermost and svc.mattermost.available else "OFFLINE"
        top = await svc.performance.leaderboard(limit=1, min_runs=1)
        best = top[0]["agent_id"] if top else "—"
        trace_summary = await svc.tracer.summary()
        typer.echo("=" * 52)
        typer.echo(f"  {svc.settings.agent_os_name.upper()}")
        typer.echo("=" * 52)
        typer.echo(f"  Executive:      ONLINE")
        typer.echo(f"  Orchestrator:   ONLINE")
        typer.echo(f"  Mattermost:     {mm}")
        typer.echo(f"  Database:       {'postgres' if svc.settings.database_url else 'in-memory'}")
        typer.echo(f"  Redis:          {'on' if svc.settings.redis_url else 'off'}")
        typer.echo("-" * 52)
        typer.echo(f"  Agents: {len(agents)} available | {len(running)} active | {len(agents) - len(active)} idle")
        typer.echo(f"  Skills: {skills} | Tools: {tools} | Models: {models} | MCP: {mcp}")
        typer.echo(f"  Projects: {projects} | Tasks: {tasks}")
        typer.echo(f"  Budget spent (month): ${total_spent:.3f}")
        typer.echo(f"  Spans traced:  {trace_summary['total_spans']} | errors: {trace_summary['errors']}")
        typer.echo(f"  Best agent:    {best}")
        typer.echo("=" * 52)

    _run(_main())


@app.command()
def health():
    """Run the full health check."""
    async def _main():
        svc = await _load_svc()
        from agentos.observability.health import HealthChecker

        reports = await HealthChecker(svc).check_all()
        for report in reports:
            mark = {"ok": "✅", "down": "❌", "degraded": "⚠️"}.get(report.status, "⚠️")
            typer.echo(f"  {mark} {report.service:<18} {report.status:<8} "
                       f"{report.latency_ms}ms  {report.detail}")
        overall = HealthChecker(svc).overall(reports)
        typer.echo(f"\nSystem health: {overall}")

    _run(_main())


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@app.command()
def agents(
    action: str = typer.Argument(..., help="list | create | disable | enable | show"),
    agent_id: Optional[str] = typer.Argument(None),
    name: Optional[str] = typer.Option(None, "--name"),
    role: Optional[str] = typer.Option(None, "--role"),
    parent: Optional[str] = typer.Option(None, "--parent"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Manage agents (list | create | disable | enable | show)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            rows = await svc.agent_registry.list()
            if json_output:
                typer.echo(json.dumps([a.model_dump() for a in rows], indent=2, default=str))
                return
            typer.echo(f"{'ID':<24} {'NAME':<28} {'ROLE':<24} {'PARENT':<20} {'TIER':<4} {'ENABLED'}")
            typer.echo("-" * 110)
            for a in rows:
                typer.echo(f"{a.id:<24} {a.name:<28} {a.role:<24} "
                           f"{(a.parent_agent or ''):<20} {a.model_policy.get('tier',''):<4} {a.enabled}")
        elif action == "create":
            from agentos.domain.models import AgentDef

            agent = AgentDef(id=agent_id or typer.prompt("agent id"),
                             name=name or typer.prompt("name"),
                             role=role or typer.prompt("role"),
                             parent_agent=parent)
            await svc.agent_registry.create(agent)
            typer.echo(f"Created agent {agent.id}")
        elif action in ("disable", "enable"):
            await svc.agent_registry.disable(agent_id or "", enabled=(action == "enable"))
            typer.echo(f"{'Enabled' if action == 'enable' else 'Disabled'} {agent_id}")
        elif action == "show":
            agent = await svc.agent_registry.get(agent_id or "")
            if not agent:
                typer.echo(f"agent {agent_id} not found")
                return
            typer.echo(json.dumps(agent.model_dump(), indent=2, default=str))

    _run(_main())


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@app.command()
def skills(action: str = typer.Argument(..., help="list | audit | show | enable | disable | search | refs"),
           query: Optional[str] = typer.Argument(None),
           category: Optional[str] = typer.Option(None, "--category")):
    """Manage skills (list | audit | show | enable | disable | search | refs)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            rows = await svc.skill_registry.list(category=category)
            typer.echo(f"{'ID':<32} {'CATEGORY':<22} {'ENABLED':<8} NAME")
            for s in rows:
                typer.echo(f"{s.id:<32} {s.category:<22} {str(s.enabled):<8} {s.name}")
        elif action == "search":
            for s in await svc.skill_registry.search(query or "", limit=15):
                typer.echo(f"  {s.id:<32} {s.name}  ({s.category})")
        elif action == "audit":
            from agentos.domain.models import SkillContract

            def has_contract(s) -> bool:
                return s.contract.model_dump() != SkillContract().model_dump()

            rows = await svc.skill_registry.list()
            total = len(rows)
            with_contract = sum(1 for s in rows if has_contract(s))
            with_tools = sum(1 for s in rows if s.tools)
            with_refs = sum(1 for s in rows if s.references)
            with_evals = 0
            for s in rows:
                if await svc.skill_registry.load_evals(s.id):
                    with_evals += 1
            verified = sum(1 for s in rows if s.contract.verification or s.contract.quality_gates)
            recovery = sum(1 for s in rows if s.contract.failure_modes)
            typer.echo(f"{'ID':<32} {'DEPT':<24} {'L':<4} {'CTX':<4} {'TLS':<4} {'REF':<4} "
                       f"{'EVL':<4} {'VER':<4} {'REC':<4} {'RISK':<8} NAME")
            typer.echo("-" * 120)
            for s in rows:
                path = s.source_path or ""
                lines = 0
                if path:
                    try:
                        lines = sum(1 for _ in open(path, errors="replace"))
                    except OSError:
                        pass
                evals = "y" if await svc.skill_registry.load_evals(s.id) else "-"
                typer.echo(f"{s.id:<32} {s.category:<24} {lines:<4} "
                           f"{'y' if has_contract(s) else '-':<4} "
                           f"{len(s.tools) or '-':<4} {len(s.references) or '-':<4} "
                           f"{evals:<4} {'y' if s.contract.verification or s.contract.quality_gates else '-':<4} "
                           f"{'y' if s.contract.failure_modes else '-':<4} {s.risk_level:<8} {s.name}")
            typer.echo("-" * 120)
            typer.echo(f"TOTAL {total} | contract {with_contract} | tools {with_tools} | "
                       f"references {with_refs} | evals {with_evals} | "
                       f"verification {verified} | recovery {recovery}")
        elif action == "show":
            s = await svc.skill_registry.get(query or "")
            if not s:
                typer.echo(f"skill {query} not found")
                return
            typer.echo(json.dumps({
                "id": s.id, "name": s.name, "category": s.category,
                "version": s.version, "risk_level": s.risk_level,
                "cost_level": s.cost_level, "tags": s.tags,
                "required_tools": s.required_tools,
                "compatible_agents": s.compatible_agents,
                "dependencies": s.dependencies,
                "contract": s.contract.model_dump(),
                "tools": [t.name for t in s.tools],
                "references": s.references,
                "source": s.source_path,
            }, indent=2))
        elif action == "refs":
            refs = await svc.skill_registry.list_references(query or "")
            if not refs:
                typer.echo(f"no references for {query}")
                return
            for name in refs:
                typer.echo(f"  {name}")
        elif action in ("enable", "disable"):
            await svc.skill_registry.enable(query or "", enabled=(action == "enable"))
            typer.echo(f"{action}d {query}")
        elif action == "reload":
            count = await svc.skill_registry.load_from_disk()
            typer.echo(f"Indexed {count} skills from disk")

    _run(_main())


# ---------------------------------------------------------------------------
# Projects / tasks
# ---------------------------------------------------------------------------

@app.command()
def projects(action: str = typer.Argument(..., help="list | create | show"),
             name: Optional[str] = typer.Argument(None),
             objective: Optional[str] = typer.Option(None, "--objective"),
             project_id: Optional[str] = typer.Option(None, "--id")):
    """Manage projects (list | create | show)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            for p in await svc.projects.list():
                typer.echo(f"{p.project_id:<18} {p.status.value:<12} {p.name} — {p.objective[:60]}")
        elif action == "create":
            project = await svc.projects.create(name or "Untitled", objective or "")
            typer.echo(f"Created {project.project_id}")
        elif action == "show":
            project = await svc.projects.get(project_id or "")
            if not project:
                typer.echo("not found")
                return
            typer.echo(json.dumps(project.model_dump(), indent=2, default=str))

    _run(_main())


@app.command()
def tasks(action: str = typer.Argument(..., help="list | create | show | retry"),
          project_id: Optional[str] = typer.Argument(None),
          title: Optional[str] = typer.Option(None, "--title"),
          agent: Optional[str] = typer.Option(None, "--agent"),
          status: Optional[str] = typer.Option(None, "--status")):
    """Manage tasks (list | create | show | retry)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            from agentos.domain.models import TaskStatus

            rows = await svc.tasks.list(status=TaskStatus(status) if status else None,
                                        project_id=project_id)
            for t in rows[:40]:
                typer.echo(f"{t.task_id:<16} {t.status.value:<16} {t.assigned_agent or '-':<20} {t.title[:50]}")
            if len(rows) > 40:
                typer.echo(f"… {len(rows) - 40} more")
        elif action == "create":
            task = await svc.tasks.create(project_id or "none", title or "Untitled",
                                          assigned_agent=agent)
            typer.echo(f"Created {task.task_id}")
        elif action == "show":
            task = await svc.tasks.get(project_id or "")
            if not task:
                typer.echo("not found")
                return
            typer.echo(json.dumps(task.model_dump(), indent=2, default=str))
        elif action == "retry":
            task = await svc.tasks.get(project_id or "")
            if not task:
                typer.echo("not found")
                return
            await svc.tasks.set_status(task.task_id, task.status)
            await svc.queue.enqueue({"task_id": task.task_id})
            typer.echo(f"Re-queued {task.task_id}")

    _run(_main())


# ---------------------------------------------------------------------------
# Models / budget / mcp / apis / workflows
# ---------------------------------------------------------------------------

@app.command()
def models():
    """List configured models."""
    async def _main():
        svc = await _load_svc()
        typer.echo(f"{'ID':<18} {'PROVIDER':<14} {'TIER':<4} {'IN$/1M':<8} {'OUT$/1M':<8} {'ENABLED'}")
        for m in await svc.model_registry.list():
            typer.echo(f"{m.id:<18} {m.provider:<14} {m.tier:<4} "
                       f"{m.price_in_per_million:<8} {m.price_out_per_million:<8} {m.enabled}")

    _run(_main())


@app.command()
def budget(action: str = typer.Argument(..., help="status | set")):
    """Budget status and limits."""
    async def _main():
        svc = await _load_svc()
        for b in await svc.budgets.summary():
            typer.echo(f"  {b['scope']:<8} {b['scope_id']:<20} "
                       f"month ${b['spent_month']:.3f}/{b['monthly_limit'] or '∞'} "
                       f"day ${b['spent_day']:.3f}/{b['daily_limit'] or '∞'}")

    _run(_main())


@app.command()
def mcp(action: str = typer.Argument(..., help="list | register"),
        name: Optional[str] = typer.Argument(None),
        endpoint: Optional[str] = typer.Option(None, "--endpoint")):
    """Manage MCP servers (list | register)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            for s in await svc.mcp_registry.list():
                typer.echo(f"{s.name:<20} {s.transport:<16} {s.endpoint or s.command or ''} "
                           f"tools={len(s.tools)} enabled={s.enabled}")
        elif action == "register":
            from agentos.domain.models import McpServer

            server = McpServer(name=name or "server", endpoint=endpoint)
            await svc.mcp_registry.register(server)
            typer.echo(f"Registered MCP server {server.name}")

    _run(_main())


@app.command()
def apis():
    """List the API catalog with evaluations."""
    async def _main():
        svc = await _load_svc()
        for api in await svc.api_registry.list():
            ev = svc.api_registry.evaluate(api)
            typer.echo(f"  {api.api:<16} {ev['verdict']:<12} score={ev['score']:<5} "
                       f"auth={api.authentication:<8} pricing={api.pricing:<8} {api.description[:60]}")

    _run(_main())


@app.command()
def workflows(action: str = typer.Argument(..., help="list | run"),
              workflow_id: Optional[str] = typer.Argument(None),
              project_id: Optional[str] = typer.Option(None, "--project"),
              goal: Optional[str] = typer.Option(None, "--goal")):
    """Manage workflows (list | run)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            for w in await svc.workflow_registry.list():
                stages = ", ".join(s.stage_id for s in w.stages)
                typer.echo(f"{w.workflow_id:<16} {w.name:<28} [{stages}]")
        elif action == "run":
            if not project_id:
                project = await svc.projects.create(goal or "Workflow project",
                                                    goal or "Run " + (workflow_id or ""))
                project_id = project.project_id
            result = await svc.engine.run_workflow(workflow_id or "", project_id)
            typer.echo(f"run {result.get('run_id')} → {result.get('status')}")

    _run(_main())


# ---------------------------------------------------------------------------
# Events / audit / approvals / memory
# ---------------------------------------------------------------------------

@app.command()
def events(limit: int = typer.Option(20, "--limit")):
    """Tail recent system events."""
    async def _main():
        svc = await _load_svc()
        for e in await svc.events.recent(limit=limit):
            typer.echo(f"  {e.ts.isoformat()[:19]} {e.type:<28} {e.severity:<7} "
                       f"{(e.agent_id or e.project_id or '')[:24]}")

    _run(_main())


@app.command()
def audit(limit: int = typer.Option(20, "--limit")):
    """Tail the audit log."""
    async def _main():
        svc = await _load_svc()
        for e in await svc.audit.query(limit=limit):
            typer.echo(f"  {e.ts.isoformat()[:19]} {e.actor:<20} {e.action:<22} "
                       f"{e.tool or '':<16} {e.result:<6} {e.target[:40]}")

    _run(_main())


@app.command()
def approvals(action: str = typer.Argument(..., help="list | approve | reject"),
              approval_id: Optional[str] = typer.Argument(None)):
    """Human approval queue (list | approve | reject)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            rows = await svc.approvals.all()
            for a in rows:
                typer.echo(f"  {a.approval_id:<18} {a.status.value:<12} {a.risk_level:<6} "
                           f"{a.action:<32} {a.agent_name}")
        elif action in ("approve", "reject"):
            decision = {"approve": "approved", "reject": "rejected"}[action]
            await svc.engine.approve(approval_id or "", decision, decided_by="cli")
            typer.echo(f"{action}d {approval_id}")

    _run(_main())


@app.command()
def policy(action: str = typer.Argument(..., help="list | evaluate"),
           agent_id: Optional[str] = typer.Option(None, "--agent"),
           tool: Optional[str] = typer.Option(None, "--tool"),
           args_json: Optional[str] = typer.Option(None, "--args"),
           env: Optional[str] = typer.Option(None, "--env")):
    """Hard security policies — the rules that override the hierarchy
    (list | evaluate)."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            rows = svc.policy.policies()
            typer.echo(f"{'ID':<30} {'KIND':<18} {'TOOLS':<20} {'ENVS':<13} ENABLED DESCRIPTION")
            typer.echo("-" * 120)
            for p in rows:
                typer.echo(f"{p.id:<30} {p.kind.value:<18} "
                           f"{','.join(p.tools) or 'all':<20} "
                           f"{','.join(p.environments) or 'all':<13} "
                           f"{str(p.enabled):<7} {p.description[:48]}")
            return
        if action == "evaluate":
            import json as _json

            agent = await svc.agent_registry.get(agent_id or "")
            if not agent:
                typer.echo(f"agent {agent_id} not found")
                return
            args = _json.loads(args_json or "{}")
            decision = await svc.policy.evaluate(agent, tool or "", args,
                                                 environment=env or svc.settings.environment)
            typer.echo(f"tool={tool} agent={agent.id} env={env or svc.settings.environment}")
            typer.echo(f"  allowed: {decision.allowed}")
            typer.echo(f"  action:  {decision.action}")
            if decision.policy_id:
                typer.echo(f"  policy:  {decision.policy_id}")
            if decision.reason:
                typer.echo(f"  reason:  {decision.reason}")
            if decision.scope:
                typer.echo(f"  scope:   {decision.scope}")

    _run(_main())


@app.command()
def emergency(action: str = typer.Argument(..., help="status | engage | disengage"),
              reason: Optional[str] = typer.Option(None, "--reason"),
              operator: str = typer.Option("cli", "--operator")):
    """The human-controlled emergency stop: engaged, EVERY tool call is
    refused until a human disengages (status | engage | disengage)."""
    async def _main():
        svc = await _load_svc()
        if action == "status":
            state = await svc.policy.emergency_status()
            if state.engaged:
                typer.echo(f"🚨 ENGAGED by {state.operator}: {state.reason or 'no reason given'}"
                           f" ({state.engaged_at})")
            else:
                typer.echo("Green — no emergency stop engaged.")
            return
        if action == "engage":
            state = await svc.policy.engage(operator, reason or "")
            typer.echo(f"🚨 EMERGENCY STOP ENGAGED by {operator}."
                       f" All tool execution is refused. Use "
                       f"`agent-os emergency disengage` to stand down.")
            return state
        if action == "disengage":
            state = await svc.policy.disengage(operator)
            typer.echo(f"✅ Emergency stop disengaged by {operator}. Tool execution resumed.")
            return state
        typer.echo("usage: agent-os emergency status|engage|disengage")

    _run(_main())


@app.command()
def workspace(action: str = typer.Argument(..., help="list | show | isolate | status | diff | integrate | discard"),
              workspace_id: Optional[str] = typer.Argument(None),
              project_id: Optional[str] = typer.Option(None, "--project"),
              task_id: Optional[str] = typer.Option(None, "--task"),
              agent_id: Optional[str] = typer.Option(None, "--agent"),
              repo: Optional[str] = typer.Option(None, "--repo")):
    """Isolated engineering workspaces (git worktrees).
    Actions: list | show | isolate | status | diff | integrate | discard."""
    async def _main():
        svc = await _load_svc()
        if action == "list":
            rows = await svc.workspaces.list()
            typer.echo(f"{'ID':<16} {'STATUS':<10} {'MODE':<8} {'AGENT':<22} TASK")
            typer.echo("-" * 90)
            for w in rows:
                typer.echo(f"{w.workspace_id:<16} {w.status.value:<10} {w.mode:<8} "
                           f"{w.agent_id:<22} {w.task_id}")
            return
        if action == "isolate":
            record = await svc.workspaces.isolate(
                project_id or "", task_id=task_id or "", agent_id=agent_id or "",
                repo_path=repo)
            typer.echo(f"Isolated {record.workspace_id} ({record.mode}) at "
                       f"{record.worktree_path}")
            return
        if action == "show":
            record = await svc.workspaces.get(workspace_id or "")
            if not record:
                typer.echo("not found")
                return
            typer.echo(json.dumps(record.model_dump(mode="json"), indent=2, default=str))
            return
        if action == "status":
            typer.echo(json.dumps(await svc.workspaces.status(workspace_id or ""),
                                  indent=2, default=str))
            return
        if action == "diff":
            typer.echo(await svc.workspaces.diff(workspace_id or "") or "(no diff)")
            return
        if action == "integrate":
            result = await svc.workspaces.integrate(workspace_id or "")
            typer.echo(json.dumps(result, indent=2, default=str))
            return
        if action == "discard":
            result = await svc.workspaces.discard(workspace_id or "")
            typer.echo(json.dumps(result, indent=2, default=str))
            return
        typer.echo("usage: agent-os workspace list|show|isolate|status|diff|integrate|discard")

    _run(_main())


@app.command()
def memory(limit: int = typer.Option(30, "--limit")):
    """List persisted memory entries."""
    async def _main():
        svc = await _load_svc()
        for m in await svc.memory.all(limit=limit):
            typer.echo(f"  [{m.scope.value:<7}] {m.owner_id:<24} {m.kind:<10} "
                       f"i={m.importance} {m.content[:80]}")

    _run(_main())


# ---------------------------------------------------------------------------
# Evaluation / performance / traces
# ---------------------------------------------------------------------------

@app.command()
def evaluate(dataset: str = typer.Argument("basic",
             help="Dataset name: eval_sets/<name>.jsonl OR a skill id with evals/cases.yaml"),
             agent: Optional[str] = typer.Option(None, "--agent", help="Override assigned agent"),
             json_output: bool = typer.Option(False, "--json")):
    """Run a benchmark over a regression dataset (or a skill's eval cases)
    through the real engine path."""
    async def _main():
        svc = await _load_svc()
        summary = await svc.evaluation.run_dataset(dataset, agent_override=agent)
        if json_output:
            typer.echo(json.dumps(summary.model_dump(), indent=2, default=str))
            return
        typer.echo(f"Dataset: {summary.dataset}  ({summary.total} tasks)")
        typer.echo(f"Passed:  {summary.passed}/{summary.total}  avg score {summary.avg_score}/5")
        typer.echo(f"Cost:    ${summary.total_cost:.4f}  avg latency {summary.avg_latency_ms}ms")
        if summary.by_fail_class:
            typer.echo(f"Failures: {summary.by_fail_class}")
        if summary.by_agent:
            typer.echo("\nBy agent:")
            for agent_id, stats in summary.by_agent.items():
                typer.echo(f"  {agent_id:<24} pass {stats['passed']}/{stats['runs']} "
                           f"score {stats['score']}")

    _run(_main())


@app.command()
def leaderboard(metric: str = typer.Option("success_rate", "--metric"),
                limit: int = typer.Option(10, "--limit"),
                group_by: str = typer.Option("agent", "--group-by",
                                             help="agent | model | skill")):
    """Show the leaderboard (success_rate | avg_cost | tool_efficiency |
    avg_review_score), grouped by agent, model or capability."""
    async def _main():
        svc = await _load_svc()
        if group_by == "agent":
            rows = await svc.performance.leaderboard(limit=limit, metric=metric)
            typer.echo(f"{'AGENT':<28} {'RUNS':<6} {'SUCCESS':<9} {'AVG COST':<10} "
                       f"{'TOOL EFF':<9} {'REVIEW':<8} {metric}")
            typer.echo("-" * 90)
            for r in rows:
                typer.echo(f"{r['agent_id']:<28} {r['runs']:<6} "
                           f"{r['success_rate'] * 100:>5.0f}%  "
                           f"${r['avg_cost']:<9.5f} {r['tool_efficiency']:<9.2f} "
                           f"{r['avg_review_score']:<8.2f} {r.get(metric)}")
            return
        from agentos.domain.models import EvaluationRecord
        from agentos.evaluation import leaderboard as eval_leaderboard

        key = {"model": "model_id", "skill": "skill_id"}.get(group_by, "agent_id")
        records = [EvaluationRecord.model_validate(r)
                   for r in await svc.entity_store.list_docs("evaluation")]
        rows = eval_leaderboard(records, group_by=key, limit=limit)
        typer.echo(f"{'KEY':<32} {'RUNS':<6} {'PASS':<9} {'SCORE':<8} {'AVG COST':<12} {'LATENCY'}")
        typer.echo("-" * 90)
        for r in rows:
            typer.echo(f"{r['key']:<32} {r['runs']:<6} "
                       f"{r['pass_rate'] * 100:>5.0f}%  {r['avg_score']:<8.2f} "
                       f"${r['avg_cost']:<11.5f} {r['avg_latency_ms']}ms")

    _run(_main())


@app.command()
def plan(goal: str = typer.Argument(..., help="The goal to plan for"),
         execute: bool = typer.Option(False, "--execute",
                                      help="Run the planned workflow immediately"),
         expand_full: bool = typer.Option(False, "--full",
                                          help="Plan the full pipeline for a major project")):
    """Show (or execute) the dynamic stage plan for a goal."""
    async def _main():
        svc = await _load_svc()
        summary = svc.planner.plan_summary(goal)
        stage_list = " → ".join(summary["stages"])
        agent_list = ", ".join(summary["agents"])
        typer.echo(f"Goal: {summary['goal']}")
        typer.echo(f"Stages ({summary['count']}): {stage_list}")
        typer.echo(f"Agents: {agent_list}")
        if execute:
            result = await svc.engine.execute_dynamic(goal, user_id="cli",
                                                      expand_full=expand_full)
            planned = ", ".join(result["planned_stages"])
            typer.echo(f"\nRun {result['run_id']} → {result['status']}")
            typer.echo(f"Planned stages: {planned}")

    _run(_main())


@app.command()
def analyze(goal: Optional[str] = typer.Argument(None,
            help="Optional goal to get a best-match recommendation for")):
    """Failure analysis + recommendations from evaluation history."""
    async def _main():
        svc = await _load_svc()
        from agentos.evaluation.failure_analysis import analyze_svc

        analysis = await analyze_svc(svc, goal or "")
        summary = analysis["failure_summary"]
        typer.echo(f"Failed runs: {summary['total_failed']}  by class: {summary['by_class']}")
        typer.echo("Recommendations:")
        for rec in analysis["recommendations"]:
            typer.echo(f"  - {rec}")
        if analysis.get("best_match"):
            bm = analysis["best_match"]
            caps = ", ".join(bm["recommended_capabilities"])
            typer.echo(f"\nTask class: {bm['task_class']}")
            typer.echo(f"Recommended agent: {bm['recommended_agent'] or 'none (insufficient history)'}")
            typer.echo(f"Recommended capabilities: {caps}")
            for c in bm["candidates"][:3]:
                typer.echo(f"  {c['agent_id']:<24} pass {c['pass_rate'] * 100:>3.0f}% "
                           f"score {c['avg_score']} cost ${c['avg_cost']:.5f}")

    _run(_main())


@app.command()
def traces(task_id: Optional[str] = typer.Argument(None, help="Task id (all recent if omitted)")):
    """Show the span chain for a task (agent → stage → model → tool → …)."""
    async def _main():
        svc = await _load_svc()
        if task_id:
            spans = await svc.tracer.task_trace(task_id)
            for s in spans:
                mark = "✅" if s["status"] == "ok" else "❌"
                typer.echo(f"  {mark} {s['kind']:<8} {s['name']:<28} "
                           f"{s['latency_ms']}ms cost=${s['cost']:.4f} "
                           f"{s.get('error') or ''}")
            return
        for s in await svc.tracer.recent(limit=20):
            typer.echo(f"  {s['kind']:<8} {s['name']:<28} {s['status']:<6} "
                       f"{s['latency_ms']}ms trace={s['trace_id'][:18]}")

    _run(_main())


@app.command()
def consolidate(scope: str = typer.Option("org", "--scope"),
                owner_id: str = typer.Option("org", "--owner")):
    """Run memory consolidation (archive stale, merge duplicates)."""
    async def _main():
        svc = await _load_svc()
        result = await svc.memory.consolidate(scope, owner_id)
        typer.echo(f"Consolidated {scope}:{owner_id} → {result}")

    _run(_main())


# ---------------------------------------------------------------------------
# Goals / demo / seed
# ---------------------------------------------------------------------------

@app.command()
def ask(goal: str = typer.Argument(..., help="The goal to give the AI organization")):
    """Give the executive a goal; it creates a project and runs the workflow."""
    async def _main():
        svc = await _load_svc()
        typer.echo(f"🧠 Executive processing: {goal[:80]}…")
        result = await svc.engine.execute_goal(goal, user_id="cli")
        typer.echo(f"  project: {result['project_id']}")
        typer.echo(f"  run:     {result['run_id']} → {result['status']}")
        if result["status"] == "awaiting_approval":
            typer.echo("  ⚠️ awaiting human approval — use `agent-os approvals list`")

    _run(_main())


@app.command()
def demo(auto_approve: bool = typer.Option(True, "--no-approve/--approve",
                                           help="Auto-approve approval gates")):
    """Run the offline end-to-end demonstration project."""
    from agentos.scripts.demo import run_demo

    _run(run_demo(auto_approve=auto_approve))


@app.command()
def seed(project: bool = typer.Option(True, "--no-project/--project",
                                      help="Also seed the demo project")):
    """Seed registries (and optionally the demo project)."""
    from agentos.scripts.seed import run_seed

    _run(run_seed(with_project=project))


def main() -> None:
    app()


if __name__ == "__main__":
    main()