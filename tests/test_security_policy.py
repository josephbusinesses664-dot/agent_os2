"""Hard Security Policy Engine tests.

Policies must ALWAYS override the hierarchy: a deny fires even for an agent
that holds the tool's permission, a require_approval forces the human gate
regardless of risk level, and the emergency stop refuses every tool call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.security.policy import PolicyEngine


@pytest.mark.asyncio
async def test_default_policies_loaded(svc):
    policies = svc.policy.policies()
    ids = {p.id for p in policies}
    assert "prod-deploy-human-approval" in ids
    assert "prod-db-writes-forbidden" in ids
    assert "no-credential-extraction" in ids
    # baked-in defaults are production-gated so development work is unaffected
    baked = [p for p in policies if p.id in (
        "prod-deploy-human-approval", "prod-db-writes-forbidden",
        "no-credential-extraction")]
    assert all(p.environments == ["production"] for p in baked)
    # the yaml-enabled hardenings are active: browser auth denial,
    # destructive-git approval, prod api read-only scope
    for expected in ("browser-never-authenticate", "destructive-git-human-approval",
                     "prod-api-readonly"):
        assert expected in ids, f"missing yaml policy {expected}"


@pytest.mark.asyncio
async def test_deny_policy_blocks_even_an_agent_who_holds_permission(svc):
    cto = await svc.agent_registry.get("cto")
    # give the CTO explicit write permission — the policy must still win
    cto.permissions["postgres.query"] = "allow"
    decision = await svc.policy.evaluate(
        cto, "postgres.query",
        {"url": "postgres://prod", "query": "UPDATE users SET plan='pro'"},
        environment="production")
    assert not decision.allowed
    assert decision.action == "deny"
    assert decision.policy_id == "prod-db-writes-forbidden"


@pytest.mark.asyncio
async def test_deny_policy_matches_only_when_args_match(svc):
    cto = await svc.agent_registry.get("cto")
    # a SELECT is not a write — the policy should not fire
    decision = await svc.policy.evaluate(
        cto, "postgres.query",
        {"url": "postgres://prod", "query": "SELECT * FROM users LIMIT 5"},
        environment="production")
    assert decision.allowed


@pytest.mark.asyncio
async def test_require_approval_policy_forces_human_gate(svc):
    cto = await svc.agent_registry.get("cto")
    cto.permissions["deploy"] = "allow"  # even with the permission…
    decision = await svc.policy.evaluate(cto, "deploy", {"env": "production"},
                                         environment="production")
    assert decision.allowed is True  # allowed, but…
    assert decision.action == "require_approval"
    assert decision.policy_id == "prod-deploy-human-approval"


@pytest.mark.asyncio
async def test_policies_silent_in_development(svc):
    cto = await svc.agent_registry.get("cto")
    decision = await svc.policy.evaluate(
        cto, "postgres.query",
        {"query": "UPDATE users SET plan='pro'"}, environment="development")
    assert decision.allowed
    assert decision.action == "allow"


@pytest.mark.asyncio
async def test_emergency_stop_refuses_everything_then_recovers(svc):
    cto = await svc.agent_registry.get("cto")
    await svc.policy.engage("human", "security incident")
    try:
        for tool, args in (("shell", {"command": "ls"}), ("deploy", {}),
                           ("filesystem.write", {"path": "x", "content": "y"})):
            decision = await svc.policy.evaluate(cto, tool, args)
            assert not decision.allowed
            assert decision.action == "emergency"
    finally:
        state = await svc.policy.disengage("human")
    assert state.engaged is False
    decision = await svc.policy.evaluate(cto, "shell", {"command": "ls"})
    assert decision.allowed


@pytest.mark.asyncio
async def test_emergency_state_persists_across_engine_instances(svc):
    await svc.policy.engage("human", "drill")
    engine2 = PolicyEngine(svc.entity_store, environment="development")
    state = await engine2.emergency_status()
    assert state.engaged is True
    assert state.operator == "human"
    await svc.policy.disengage("human")


@pytest.mark.asyncio
async def test_yaml_adds_and_disables_policies(svc, tmp_path):
    yaml_file = tmp_path / "policies.yaml"
    yaml_file.write_text(
        "security_policies:\n"
        "  - id: custom-deny\n"
        "    kind: deny_tool\n"
        "    tools: [browser.evaluate]\n"
        "    reason: test policy\n"
        "disabled: [no-credential-extraction]\n")
    engine = PolicyEngine(svc.entity_store, policies_path=str(yaml_file),
                          environment="production")
    ids = {p.id for p in engine.policies()}
    assert "custom-deny" in ids
    assert "no-credential-extraction" not in ids  # explicitly disabled
    assert "prod-db-writes-forbidden" in ids       # defaults still enforced


@pytest.mark.asyncio
async def test_browser_auth_policy_denies_login_flows(svc):
    executive = await svc.agent_registry.get("executive")
    decision = await svc.policy.evaluate(
        executive, "browser.type",
        {"selector": "#user", "text": "admin"})
    assert not decision.allowed
    assert decision.policy_id == "browser-never-authenticate"
    # ordinary research interactions stay allowed
    decision = await svc.policy.evaluate(executive, "browser.click",
                                         {"selector": "#go"})
    assert decision.allowed


@pytest.mark.asyncio
async def test_destructive_git_requires_human_approval(svc):
    cto = await svc.agent_registry.get("cto")
    # ordinary read-only github calls are unaffected
    decision = await svc.policy.evaluate(cto, "github",
                                         {"action": "get_repo", "repo": "a/b"})
    assert decision.allowed and decision.action == "allow"
    # rewriting history forces the human gate, even for the CTO
    decision = await svc.policy.evaluate(
        cto, "github", {"action": "push", "force": "true"})
    assert decision.allowed is True
    assert decision.action == "require_approval"
    assert decision.policy_id == "destructive-git-human-approval"


@pytest.mark.asyncio
async def test_prod_api_readonly_restricts_scope(svc):
    cto = await svc.agent_registry.get("cto")
    decision = await svc.policy.evaluate(cto, "api.call", {"api": "stripe"},
                                         environment="production")
    assert decision.action == "restrict_scope"
    assert decision.scope == "read-only"
    # development api calls are untouched
    decision = await svc.policy.evaluate(cto, "api.call", {"api": "stripe"},
                                         environment="development")
    assert decision.action == "allow"


@pytest.mark.asyncio
async def test_executor_enforces_policy_above_permissions(svc, tmp_path):
    """The full execution path: even an agent that HOLD the postgres.query
    permission cannot write to the production database, and the emergency stop
    refuses every tool call through the executor."""
    from agentos.config import Settings
    from agentos.orchestration.engine import OrchestratorEngine
    from agentos.services import Services

    settings = Settings(database_url=None, redis_url=None,
                        workspace_dir=str(tmp_path / "ws"), environment="production")
    prod = Services(settings)
    await prod.seed()
    prod.engine = OrchestratorEngine(prod)
    try:
        cto = await prod.agent_registry.get("cto")
        cto.permissions["postgres.query"] = "allow"  # agent permission granted
        cto.permissions["shell"] = "allow"
        project = await prod.projects.create("prod-test", "test")
        task = await prod.tasks.create(
            project.project_id, "write to prod db", "update users",
            assigned_agent=cto.id)
        ctx = prod.runtime(cto, task, project).ctx

        # permission says allow — the hard policy must still deny the write
        result = await prod.executor.execute(
            ctx, cto, "postgres.query",
            {"url": "postgres://prod", "query": "DROP TABLE users"})
        assert not result.get("ok")
        assert "hard security policy" in result.get("error", "")

        # emergency stop refuses even harmless tools through the executor
        await prod.policy.engage("human", "test")
        try:
            result = await prod.executor.execute(ctx, cto, "shell",
                                                 {"command": "echo hi"})
            assert not result.get("ok")
            assert "EMERGENCY STOP" in result.get("error", "")
        finally:
            await prod.policy.disengage("human")
        # after stand-down the same low-risk call succeeds again
        result = await prod.executor.execute(ctx, cto, "filesystem.write",
                                             {"path": "recovered.txt", "content": "ok"})
        assert result.get("ok"), result
    finally:
        await prod.close()