"""Tool Executor — the single execution path for every tool call.

Enforces, technically, in order:
  1. permission policy (agent's allow/deny map, with `allow:scope` support),
  2. approval gate for high-risk tools (no approval → no execution),
  3. sandbox boundary (handlers validate paths; executor validates nothing
     extra — handlers own their sandbox),
  4. timeout per tool,
  5. retries for transient failures with a strategy change (arg coercion /
     narrowed re-invocation), never an identical infinite retry,
  6. capability hooks + validators from active skills,
  7. telemetry: every call records a trace span (latency, cost, ok/error).

All calls are audit-logged with secrets redacted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from agentos.registries.tool_registry import ToolRegistry
from agentos.security.approvals import ApprovalService
from agentos.security.audit import AuditLog

logger = logging.getLogger("agentos.tools")

_SENSITIVE_KEYS = {"token", "key", "password", "secret", "api_key", "authorization", "cookie"}
_WRITE_CMDS = __import__("re").compile(r"(;|&&|\|\||>|\brm\b|\bmv\b|\bmkdir\b|"
                                       r"curl\s+-X|git\s+push|git\s+commit|"
                                       r"docker\s+compose\s+up|pip\s+install\s|npm\s+i\s)")

# Least privilege by default: tools that touch the network, external systems,
# credentials, deployment or the outside world are DENIED unless the agent
# explicitly lists them. The org chart grants these selectively; agents
# created ad-hoc (CLI/API) get no implicit access.
_DEFAULT_DENY = {
    "shell", "web.scrape", "api.call", "mcp.call", "deploy", "github",
    "postgres.query", "docker", "agent.delegate",
    "browser.open", "browser.snapshot", "browser.click", "browser.type",
    "browser.screenshot", "browser.evaluate", "browser.close",
}


class ToolDeniedError(PermissionError):
    pass


def redact_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS) and v:
            out[k] = f"<redacted:{len(str(v))} chars>"
        else:
            out[k] = str(v)[:200]
    return out


# tools that mutate state (rollback ledger records successful calls)
_MUTATIVE_TOOLS = {"shell", "api.call", "mcp.call", "mattermost.post", "deploy",
                   "github", "docker", "filesystem.write", "file.patch"}
# workspace-confined writes have native undo (compensation deletes the file)
_SANDBOXED_TOOLS = {"filesystem.write", "file.patch"}


class ToolExecutor:
    def __init__(self, tools: ToolRegistry, approvals: ApprovalService,
                 audit: AuditLog, *, require_approval_risk: str = "high",
                 rollback: Any = None,
                 tracer: Any = None, capabilities: Any = None,
                 retry_transient: int = 2) -> None:
        self.tools = tools
        self.approvals = approvals
        self.audit = audit
        self.require_approval_risk = require_approval_risk
        self.rollback = rollback
        self.tracer = tracer
        self.capabilities = capabilities
        self.retry_transient = retry_transient

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def execute(self, ctx: Any, agent: Any, tool_name: str, args: dict) -> dict:
        tool = await self.tools.get(tool_name)
        if tool is None or not tool.enabled:
            # help the model recover: suggest the closest real tool so the
            # next call is likely to work instead of being a dead end
            hint = ""
            try:
                import difflib

                names = [t.name for t in await self.tools.list(enabled_only=True)]
                close = difflib.get_close_matches(tool_name, names, n=1, cutoff=0.45)
                if close:
                    hint = f" — did you mean `{close[0]}`?"
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "error": f"unknown or disabled tool: {tool_name}{hint}"}

        # 0. HARD SECURITY POLICIES — evaluated BEFORE the agent's own
        #    permissions, so they always override the hierarchy. A matching
        #    deny blocks even an executive; require_approval forces the human
        #    gate regardless of risk level; restrict_scope coerces the call
        #    into a narrower permission scope. Emergency stop refuses all.
        policy = getattr(ctx, "services", None) and getattr(ctx.services, "policy", None)
        environment = (getattr(ctx.services, "settings", None)
                       and getattr(ctx.services.settings, "environment", "development")) \
            if ctx.services is not None else "development"
        policy_decision = await policy.evaluate(agent, tool_name, args,
                                                environment=environment) \
            if policy is not None else None
        policy_scope: Optional[str] = None
        if policy_decision is not None and not policy_decision.allowed:
            action = "tool.emergency_blocked" if policy_decision.action == "emergency" \
                else "tool.denied"
            await self._audit(ctx, agent, tool_name, action, "denied",
                              {"reason": policy_decision.reason,
                               "policy_id": policy_decision.policy_id})
            return {"ok": False, "error": policy_decision.reason}
        if policy_decision is not None and policy_decision.action == "restrict_scope":
            policy_scope = policy_decision.scope

        # 1. permission policy (allow | deny | allow:scope); explicit entries
        #    win, otherwise high-risk tools default to deny (least privilege).
        #    A hard policy may coerce the scope (e.g. shell -> read-only).
        permission_key = tool.permission_key or tool.name
        allowed = agent.permissions.get(
            permission_key, "deny" if permission_key in _DEFAULT_DENY else "allow")
        if policy_scope:
            allowed = f"allow:{policy_scope}"
        if allowed == "deny":
            await self._audit(ctx, agent, tool_name, "tool.denied", "denied",
                              {"reason": "permission policy"})
            return {"ok": False, "error": f"tool {tool_name} denied by permission policy for {agent.id}"}
        scope = allowed.split(":", 1)[1] if ":" in allowed else None
        if tool_name == "shell" and scope == "read-only" and _WRITE_CMDS.search(args.get("command", "")):
            await self._audit(ctx, agent, tool_name, "tool.denied", "denied",
                              {"reason": "read-only shell scope"})
            return {"ok": False, "error": "shell write command denied (read-only scope)"}

        # 2. approval gate for high-risk tools (and hard-policy-forced
        #    approvals — a require_approval policy holds even when the tool
        #    itself is low risk and the agent holds the permission)
        requires_approval = (tool.risk_level == self.require_approval_risk
                             and tool.risk_level == "high")
        forced_approval = None
        if policy_decision is not None and policy_decision.action == "require_approval":
            requires_approval = True
            forced_approval = policy_decision
        if requires_approval:
            granted = await self._tool_approved(ctx, tool_name)
            if not granted:
                approval = await self.approvals.request(
                    agent.id, agent.name, f"tool:{tool_name}",
                    task_id=ctx.task.task_id if ctx.task else None,
                    project_id=ctx.project.project_id if ctx.project else None,
                    risk_level=forced_approval.approval_risk if forced_approval else "high",
                    reason=(f"{agent.name} wants to call {tool_name} with args "
                            f"{json.dumps(redact_args(args))[:300]}"
                            + (f" — {forced_approval.reason}" if forced_approval else "")),
                )
                await self._audit(ctx, agent, tool_name, "tool.approval_requested", "pending",
                                  {"approval_id": approval.approval_id})
                return {"ok": False, "pending_approval": approval.approval_id,
                        "error": f"tool {tool_name} requires human approval "
                                 f"({approval.approval_id}) — awaiting decision"}

        handler = self.tools.handler(tool_name)
        if handler is None:
            return {"ok": False, "error": f"no handler registered for {tool_name}"}

        # capability pre-hooks (from active skills) may adjust args
        active_skills = getattr(ctx, "active_skills", []) or []
        if self.capabilities is not None:
            for hook in self.capabilities.pre_hooks(active_skills, tool_name):
                try:
                    hooked = await hook(ctx, args)
                    if isinstance(hooked, dict) and "args" in hooked:
                        args = {**args, **hooked["args"]}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": f"pre-hook for {tool_name} failed: {exc}"}

        timeout = float(tool.config.get("timeout_seconds", 30) or 30)
        span_ctx = None
        if self.tracer is not None:
            span_ctx = self.tracer.span(
                kind="tool", name=f"tool:{tool_name}", trace_id=ctx.task.task_id if ctx.task else "tool",
                parent_span=getattr(ctx, "current_span", None),
                agent_id=agent.id, task_id=ctx.task.task_id if ctx.task else None,
                project_id=ctx.project.project_id if ctx.project else None,
                tool=tool_name,
                payload={"args": redact_args(args)})
            span = await span_ctx.__aenter__()
        start = time.perf_counter()
        result = await self._call_with_retries(handler, ctx, args, tool_name, timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if span_ctx is not None:
            span.latency_ms = latency_ms
            span.status = "ok" if result.get("ok") else "error"
            span.error = result.get("error")
            span.cost = float(tool.config.get("cost_per_call", 0) or 0)
            span.result_summary = json.dumps(result)[:300]
            await span_ctx.__aexit__(None, None, None)

        # capability post-hooks + validators
        if self.capabilities is not None:
            for hook in self.capabilities.post_hooks(active_skills, tool_name):
                try:
                    await hook(ctx, args, result)
                except Exception:  # noqa: BLE001
                    pass
            result = await self.capabilities.validate_result(active_skills, ctx, tool_name, result)

        # cost tracking (tool-level, folded into performance + traces)
        cost_per_call = float(tool.config.get("cost_per_call", 0) or 0)
        if cost_per_call > 0:
            result["cost"] = cost_per_call

        # rollback/compensation ledger (Phase 14): record every successful
        # mutative action; snapshot files before workspace writes so the
        # compensation can restore them. Recording is best-effort — a ledger
        # failure never breaks execution.
        if result.get("ok") and tool_name in _MUTATIVE_TOOLS:
            try:
                entry = self.rollback.record(
                    tool=tool_name, args=args, agent_id=agent.id,
                    task_id=ctx.task.task_id if ctx.task else None,
                    project_id=ctx.project.project_id if ctx.project else None,
                    reversibility="reversible" if tool_name in _SANDBOXED_TOOLS else "compensating",
                    compensation_name=("compensate_filesystem_write"
                                       if tool_name in _SANDBOXED_TOOLS else ""),
                    compensation_args=({"workspace": str(ctx.workspace)}
                                       if tool_name in _SANDBOXED_TOOLS else None),
                    note="recorded by executor")
                result["rollback_entry"] = entry.entry_id
                result["reversibility"] = entry.reversibility
            except Exception:  # noqa: BLE001
                pass

        await self._audit(ctx, agent, tool_name, "tool.called",
                          "ok" if result.get("ok") else "error",
                          {"args": redact_args(args), "result": str(result)[:500],
                           "latency_ms": latency_ms})
        return result

    async def _call_with_retries(self, handler: Any, ctx: Any, args: dict,
                                 tool_name: str, timeout: float) -> dict:
        attempt = 0
        while True:
            result = await self._call_once(handler, ctx, args, timeout)
            if result.get("ok") or attempt >= self.retry_transient or tool_name in (
                    "filesystem.write", "file.patch", "memory.save", "mattermost.post"):
                return result
            error = result.get("error", "")
            if not _is_transient(error):
                return result
            # strategy change, not identical retry: coerce numeric args
            coerced = _coerce_args(args)
            if coerced != args:
                args = coerced
            attempt += 1
            await asyncio.sleep(min(2 ** attempt, 5))
        return result

    async def _call_once(self, handler: Any, ctx: Any, args: dict, timeout: float) -> dict:
        try:
            if timeout > 0:
                return await asyncio.wait_for(handler(ctx, args), timeout=timeout)
            return await handler(ctx, args)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"tool timed out after {timeout}s"}
        except PermissionError as exc:
            return {"ok": False, "error": f"permission denied: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool call failed")
            return {"ok": False, "error": f"{exc}"}

    # ------------------------------------------------------------------
    # Approval helpers
    # ------------------------------------------------------------------
    async def _tool_approved(self, ctx: Any, tool_name: str) -> bool:
        task_id = ctx.task.task_id if ctx.task else "none"
        if getattr(ctx, "approved_tools", None) is not None and tool_name in ctx.approved_tools:
            return True
        pending = await self.approvals.pending()
        for approval in pending:
            if approval.task_id == task_id and approval.action == f"tool:{tool_name}":
                return False  # already awaiting
        if ctx.services.memory is not None:
            entries = await ctx.services.memory.recall("task", task_id, query=f"approve {tool_name}", limit=5)
            for entry in entries:
                if entry.kind == "approval" and f"approve:{tool_name}" in entry.content:
                    return True
        return False

    async def _audit(self, ctx: Any, agent: Any, tool_name: str, action: str,
                     result: str, details: dict) -> None:
        try:
            await self.audit.record(
                agent.id, action, target=tool_name,
                project_id=ctx.project.project_id if ctx.project else None,
                task_id=ctx.task.task_id if ctx.task else None,
                tool=tool_name, result=result, details=details)
        except Exception:  # noqa: BLE001
            logger.exception("audit write failed")


def _is_transient(error: str) -> bool:
    low = error.lower()
    return any(k in low for k in ("timeout", "timed out", "connection", "reset",
                                  "502", "503", "temporarily", "try again"))


def _coerce_args(args: dict) -> dict:
    """Strategy-change retry: coerce string numerics so a model that passed
    strings where ints were expected gets a working second attempt."""
    out = dict(args)
    for key, value in list(args.items()):
        if isinstance(value, str):
            try:
                if value.lstrip("-").isdigit():
                    out[key] = int(value)
                else:
                    float(value)
                    out[key] = float(value)
            except ValueError:
                pass
    return out