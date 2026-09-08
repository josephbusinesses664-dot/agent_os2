"""Tool/MCP upgrade tests: capability discovery, technical permission
scopes, strategy-change retries, timeouts, secret redaction, health checks."""

from __future__ import annotations

import asyncio

import pytest

from agentos.domain.models import AgentDef, CapabilityTool, ToolDef
from agentos.tools.executor import redact_args


@pytest.mark.asyncio
async def test_tool_discovery_ranks_by_relevance(svc):
    tools = await svc.tool_registry.discover("search the web for research", limit=3)
    assert tools
    assert tools[0].name == "web.search"


@pytest.mark.asyncio
async def test_discovery_respects_agent_permissions(svc):
    restricted = AgentDef(id="no-web", name="No Web", role="test",
                          permissions={"web.search": "deny"})
    tools = await svc.tool_registry.discover("search the web", agent=restricted, limit=20)
    assert "web.search" not in [t.name for t in tools]


@pytest.mark.asyncio
async def test_shell_read_only_scope_enforced(svc):
    """allow:read-only must technically block write commands."""
    agent = AgentDef(id="ro-agent", name="RO", role="test",
                     permissions={"shell": "allow:read-only"})
    project = await svc.projects.create("ro-test", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project, approved_tools={"shell"}).ctx
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    result = await svc.executor.execute(ctx, agent, "shell", {"command": "ls"})
    assert result["ok"], result
    denied = await svc.executor.execute(ctx, agent, "shell", {"command": "rm -rf ."})
    assert not denied["ok"]
    assert "read-only" in denied["error"]


@pytest.mark.asyncio
async def test_transient_retry_with_strategy_change(svc):
    """A transient failure retries with coerced args (strategy change)."""
    calls = {"n": 0}

    async def flaky(ctx, args):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "temporarily unavailable, try again"}
        return {"ok": True, "got": args["count"]}

    await svc.tool_registry.register(
        ToolDef(name="flaky.read", description="flaky read", risk_level="low"),
        flaky)
    agent = await svc.agent_registry.get("executive")
    project = await svc.projects.create("retry-test", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project).ctx
    result = await svc.executor.execute(ctx, agent, "flaky.read", {"count": "5"})
    assert result["ok"]
    assert result["got"] == 5  # coerced string → int on the retry
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_non_transient_failure_not_retried(svc):
    calls = {"n": 0}

    async def always_bad(ctx, args):
        calls["n"] += 1
        return {"ok": False, "error": "logic bug in handler"}

    await svc.tool_registry.register(
        ToolDef(name="bad.read", description="bad", risk_level="low"), always_bad)
    agent = await svc.agent_registry.get("executive")
    project = await svc.projects.create("no-retry", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project).ctx
    result = await svc.executor.execute(ctx, agent, "bad.read", {})
    assert not result["ok"]
    assert calls["n"] == 1  # no retry for logic failures


@pytest.mark.asyncio
async def test_tool_timeout_enforced(svc):
    async def slow(ctx, args):
        await asyncio.sleep(5)
        return {"ok": True}

    await svc.tool_registry.register(
        ToolDef(name="slow.read", description="slow", risk_level="low",
                config={"timeout_seconds": 0.2}), slow)
    agent = await svc.agent_registry.get("executive")
    project = await svc.projects.create("timeout-test", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project).ctx
    result = await svc.executor.execute(ctx, agent, "slow.read", {})
    assert not result["ok"]
    assert "timed out" in result["error"]


def test_redact_args_scrubs_secrets():
    out = redact_args({"api_key": "sk-1234567890", "query": "hello", "password": "hunter2"})
    assert "sk-1234567890" not in str(out)
    assert out["query"] == "hello"
    assert "redacted" in out["api_key"]


@pytest.mark.asyncio
async def test_tool_health_reports(svc):
    report = await svc.tool_registry.health()
    assert report["status"] == "ok"
    assert report["total"] > 0
    one = await svc.tool_registry.health("repo.search")
    assert one["status"] == "ok"


@pytest.mark.asyncio
async def test_mcp_lifecycle_and_discovery(svc):
    from agentos.domain.models import McpServer

    server = McpServer(name="fake-server", endpoint="http://localhost:1/nope",
                       tools=["ping"])
    await svc.mcp_registry.register(server)
    health = await svc.mcp_registry.health_check("fake-server")
    assert health["status"] == "error"  # unreachable endpoint fails cleanly
    tools = await svc.mcp_registry.discover_tools("fake-server")
    assert tools == []  # discovery failure is contained, not an exception
    await svc.mcp_registry.disconnect("fake-server")


@pytest.mark.asyncio
async def test_executor_records_tool_spans(svc):
    agent = await svc.agent_registry.get("executive")
    project = await svc.projects.create("span-test", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project).ctx
    await svc.executor.execute(ctx, agent, "calculator", {"expression": "2+2"})
    spans = await svc.tracer.task_trace(task.task_id)
    tool_spans = [s for s in spans if s["kind"] == "tool"]
    assert tool_spans, spans
    assert tool_spans[0]["tool"] == "calculator"


@pytest.mark.asyncio
async def test_missing_required_args_return_structured_error(svc):
    """A tool call missing declared required params must return a structured
    error naming them — never a raw KeyError crash (models occasionally emit
    empty/partial arg dicts)."""
    agent = await svc.agent_registry.get("executive")
    project = await svc.projects.create("args-test", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project).ctx

    # filesystem.read requires 'path'
    result = await svc.executor.execute(ctx, agent, "filesystem.read", {})
    assert not result["ok"]
    assert "path" in result["error"]
    assert "KeyError" not in result["error"]

    # db.query requires 'db' and 'query'
    result = await svc.executor.execute(ctx, agent, "db.query", {"db": "x.db"})
    assert not result["ok"]
    assert "query" in result["error"]

    # shell requires 'command' (approve the high-risk tool so the call
    # reaches arg validation)
    shell_ctx = svc.runtime(agent, task, project, approved_tools={"shell"}).ctx
    result = await svc.executor.execute(shell_ctx, agent, "shell", {})
    assert not result["ok"]
    assert "command" in result["error"]

    # optional params are not demanded (repo.tree has no required)
    result = await svc.executor.execute(ctx, agent, "repo.tree", {})
    assert result["ok"]

    # a valid call still works
    result = await svc.executor.execute(ctx, agent, "calculator", {"expression": "6*7"})
    assert result["ok"]
    assert str(result["result"]) == "42"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_fails_fast(svc, monkeypatch):
    """Repeated execution failures must open the tool's circuit and make
    subsequent calls fail fast with a structured error instead of hammering
    the broken backend (§7 circuit breakers / health state)."""
    import agentos.tools.executor as ex

    monkeypatch.setattr(ex, "_CIRCUIT_FAIL_THRESHOLD", 3)
    monkeypatch.setattr(ex, "_CIRCUIT_COOLDOWN_SECONDS", 60.0)

    calls = {"n": 0}

    async def broken(ctx, args):
        calls["n"] += 1
        return {"ok": False, "error": "backend down"}

    await svc.tool_registry.register(
        ToolDef(name="flaky.circuit", description="flaky", risk_level="low"), broken)
    agent = await svc.agent_registry.get("executive")
    project = await svc.projects.create("circuit-test", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project).ctx

    # threshold failures execute normally (each fails), then the circuit opens
    for _ in range(3):
        result = await svc.executor.execute(ctx, agent, "flaky.circuit", {})
        assert not result["ok"]
    assert calls["n"] == 3

    # circuit open → fail fast, handler NOT invoked
    result = await svc.executor.execute(ctx, agent, "flaky.circuit", {})
    assert not result["ok"]
    assert "circuit open" in result["error"]
    assert calls["n"] == 3

    # health view exposes the open circuit
    circuits = svc.executor._circuit_status()
    assert circuits.get("flaky.circuit") == "open"

    # cooldown expiry → half-open probe: one call reaches the handler
    svc.executor._circuits["flaky.circuit"]["open_until"] = 0  # force expiry
    result = await svc.executor.execute(ctx, agent, "flaky.circuit", {})
    assert not result["ok"]
    assert calls["n"] == 4  # the probe reached the (still broken) handler
    assert svc.executor._circuit_status()["flaky.circuit"] == "open"  # re-opened

    # and it fails fast again without touching the handler
    result = await svc.executor.execute(ctx, agent, "flaky.circuit", {})
    assert "circuit open" in result["error"]
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_on_success(svc, monkeypatch):
    """A successful call resets the circuit (closed) and clears the failure
    count — a transiently broken tool recovers without intervention."""
    import agentos.tools.executor as ex

    monkeypatch.setattr(ex, "_CIRCUIT_FAIL_THRESHOLD", 2)
    calls = {"n": 0}

    async def flaky_ok(ctx, args):
        calls["n"] += 1
        return {"ok": calls["n"] > 2, "error": "" if calls["n"] > 2 else "down"}

    await svc.tool_registry.register(
        ToolDef(name="recover.circuit", description="flaky", risk_level="low"), flaky_ok)
    agent = await svc.agent_registry.get("executive")
    project = await svc.projects.create("circuit-recover", "t")
    task = await svc.tasks.create(project.project_id, "t", "d")
    ctx = svc.runtime(agent, task, project).ctx

    # 2 failures open the circuit
    await svc.executor.execute(ctx, agent, "recover.circuit", {})
    await svc.executor.execute(ctx, agent, "recover.circuit", {})
    assert svc.executor._circuit_status()["recover.circuit"] == "open"

    # force cooldown expiry → half-open probe succeeds → circuit closes
    # (a fully healthy tool has no circuit entry at all)
    svc.executor._circuits["recover.circuit"]["open_until"] = 0
    result = await svc.executor.execute(ctx, agent, "recover.circuit", {})
    assert result["ok"]
    assert "recover.circuit" not in svc.executor._circuit_status()

    # and the next call is a normal (non-probe) execution
    result = await svc.executor.execute(ctx, agent, "recover.circuit", {})
    assert result["ok"]
    assert calls["n"] == 4