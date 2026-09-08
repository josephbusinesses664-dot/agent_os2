"""Hard Security Policy Engine.

HARD SECURITY POLICIES ALWAYS OVERRIDE THE HIERARCHY (master spec §5 / §25).

The org chart grants *authority*; policies *bound* it. A policy is a
declarative rule evaluated on every tool call BEFORE the agent's own
permissions, so no agent — including executives — can grant itself something
a policy forbids. Enforcement happens in the ToolExecutor, above the
permission/approval chain: a denied call never reaches the handler.

Includes the emergency stop: a human-engaged global brake that refuses every
tool call until a human disengages it. No agent can lift it; only an operator
(human via Mattermost / API / CLI) can.

Policies ship with immutable in-code defaults (production-gated) and can be
extended by `config/policies.yaml`. Policy contents cannot be changed at
runtime — only the emergency flag is a runtime control, and only for humans.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from agentos.domain.models import PolicyKind, SecurityPolicy

logger = logging.getLogger("agentos.policy")

# ---------------------------------------------------------------------------
# Default policies — baked into the platform, always enforced. Gated to the
# production environment so development/offline runs are unaffected; in
# production they cannot be disabled at runtime.
# ---------------------------------------------------------------------------
_DEFAULT_POLICIES: list[dict[str, Any]] = [
    {
        "id": "prod-deploy-human-approval",
        "kind": "require_approval",
        "description": "Deploying to production always requires human approval. "
                       "No agent — including executives — may self-approve a "
                       "production deployment.",
        "tools": ["deploy"],
        "environments": ["production"],
        "approval_risk": "high",
        "reason": "production deployments require human approval (hard policy)",
    },
    {
        "id": "prod-db-writes-forbidden",
        "kind": "deny_tool",
        "description": "Write/DML statements against the production database are "
                       "prohibited for every agent, regardless of hierarchy.",
        "tools": ["postgres.query"],
        "environments": ["production"],
        "arg_pattern": r"(?i)\b(insert|update|delete|drop|alter|truncate|grant|"
                       r"revoke|create)\b",
        "reason": "production database writes are forbidden by hard security policy",
    },
    {
        "id": "no-credential-extraction",
        "kind": "deny_tool",
        "description": "In production, tools may not be used to exfiltrate "
                       "credentials or secrets from the environment.",
        "tools": ["shell", "api.call", "web.scrape", "github"],
        "environments": ["production"],
        "arg_pattern": r"(?i)\b(password|secret|api[_-]?key|access[_-]?token|"
                       r"authorization|credential)\b",
        "reason": "credential extraction is forbidden by hard security policy",
    },
]


class PolicyDecision(BaseModel):
    """Result of evaluating a tool call against the policy engine."""

    allowed: bool = True
    action: str = "allow"  # allow | deny | require_approval | restrict_scope | emergency
    policy_id: Optional[str] = None
    reason: str = ""
    scope: Optional[str] = None
    approval_risk: str = "high"


class EmergencyState(BaseModel):
    engaged: bool = False
    operator: str = ""
    reason: str = ""
    engaged_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["engaged_at"] = self.engaged_at.isoformat() if self.engaged_at else None
        return d


class PolicyEngine:
    """Evaluates hard security policies over agent tool calls.

    Order of precedence (first hit wins):
      1. emergency stop  — everything is refused, no exceptions
      2. deny_tool       — the call is blocked outright
      3. restrict_scope  — the call runs, coerced into a narrower scope
      4. require_approval — the call runs only after human approval
    """

    def __init__(self, store: Any, *, policies_path: Optional[str] = None,
                 environment: str = "development",
                 event_bus: Any = None, audit: Any = None) -> None:
        self.store = store
        self.policies_path = policies_path
        self.environment = environment
        self.event_bus = event_bus
        self.audit = audit
        self._policies: list[SecurityPolicy] = []
        self._disabled: set[str] = set()
        self.load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self) -> list[SecurityPolicy]:
        """(Re)load policies: in-code defaults + config/policies.yaml extras.

        YAML can add policies or explicitly disable a default by id
        (`disabled: [id]`) — that opt-out is deliberate and logged loudly.
        """
        policies = [SecurityPolicy.model_validate(p) for p in _DEFAULT_POLICIES]
        self._disabled = set()
        if self.policies_path:
            extras = self._load_yaml(self.policies_path)
            policies.extend(SecurityPolicy.model_validate(p)
                            for p in (extras.get("security_policies") or []))
            self._disabled = set(extras.get("disabled") or [])
        if self._disabled:
            logger.warning("hard security policies explicitly disabled in %s: %s",
                           self.policies_path or "config", sorted(self._disabled))
        self._policies = [p for p in policies if p.id not in self._disabled]
        return self._policies

    def _load_yaml(self, path: str) -> dict[str, Any]:
        try:
            import yaml

            from pathlib import Path

            p = Path(path)
            if not p.exists():
                return {}
            with p.open() as fh:
                data = yaml.safe_load(fh) or {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load security policies from %s: %s — "
                           "defaults only", path, exc)
            return {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def policies(self) -> list[SecurityPolicy]:
        return list(self._policies)

    async def evaluate(self, agent: Any, tool_name: str, args: dict,
                       environment: Optional[str] = None) -> PolicyDecision:
        """Evaluate one tool call. Always returns a decision — never raises."""
        env = environment or self.environment
        # 1. emergency stop: everything is refused, no exceptions
        emergency = await self.emergency_status()
        if emergency.engaged:
            return PolicyDecision(
                allowed=False, action="emergency",
                reason=(f"EMERGENCY STOP ENGAGED by {emergency.operator}: "
                        f"{emergency.reason or 'all tool execution halted'}"),
            )
        # 2-4. matching policies in precedence order
        restrict: Optional[SecurityPolicy] = None
        approval: Optional[SecurityPolicy] = None
        for policy in self._policies:
            if not self._matches(policy, agent, tool_name, args, env):
                continue
            if policy.kind == PolicyKind.DENY_TOOL:
                return PolicyDecision(
                    allowed=False, action="deny", policy_id=policy.id,
                    reason=policy.reason or
                           f"tool {tool_name} blocked by hard security policy "
                           f"{policy.id}",
                )
            if policy.kind == PolicyKind.RESTRICT_SCOPE and restrict is None:
                restrict = policy
            if policy.kind == PolicyKind.REQUIRE_APPROVAL and approval is None:
                approval = policy
        if restrict is not None:
            return PolicyDecision(
                allowed=True, action="restrict_scope", policy_id=restrict.id,
                reason=restrict.description, scope=restrict.scope,
            )
        if approval is not None:
            return PolicyDecision(
                allowed=True, action="require_approval", policy_id=approval.id,
                reason=approval.description or approval.reason,
                approval_risk=approval.approval_risk,
            )
        return PolicyDecision()

    def _matches(self, policy: SecurityPolicy, agent: Any, tool_name: str,
                 args: dict, environment: str) -> bool:
        if not policy.enabled:
            return False
        if policy.tools and tool_name not in policy.tools:
            return False
        if policy.agents and agent.id not in policy.agents:
            return False
        if policy.roles and agent.role not in policy.roles:
            return False
        if policy.environments and environment not in policy.environments:
            return False
        if policy.arg_pattern:
            try:
                if not re.search(policy.arg_pattern, json.dumps(args, default=str)):
                    return False
            except re.error:
                return False
        return True

    # ------------------------------------------------------------------
    # Emergency stop (human-only control)
    # ------------------------------------------------------------------
    async def engage(self, operator: str, reason: str = "") -> EmergencyState:
        """Human engages the emergency stop. Blocks ALL tool execution until
        disengaged. No agent can call this through the executor — the tool
        layer refuses tool calls while engaged, so only operators (API/CLI/
        Mattermost human commands) reach it."""
        state = EmergencyState(engaged=True, operator=operator, reason=reason,
                               engaged_at=datetime.now(timezone.utc))
        await self._save_emergency(state)
        await self._emit("security.emergency_engaged",
                         {"operator": operator, "reason": reason},
                         severity="critical")
        if self.audit is not None:
            try:
                await self.audit.record(operator, "security.emergency_engaged",
                                        target="all", result="ok",
                                        details={"reason": reason})
            except Exception:  # noqa: BLE001
                pass
        return state

    async def disengage(self, operator: str) -> EmergencyState:
        state = EmergencyState(engaged=False, operator=operator)
        await self._save_emergency(state)
        await self._emit("security.emergency_disengaged",
                         {"operator": operator}, severity="warning")
        if self.audit is not None:
            try:
                await self.audit.record(operator, "security.emergency_disengaged",
                                        target="all", result="ok")
            except Exception:  # noqa: BLE001
                pass
        return state

    async def emergency_status(self) -> EmergencyState:
        doc = await self.store.get_doc("policy_control", "emergency")
        if not doc:
            return EmergencyState()
        return EmergencyState.model_validate(doc)

    async def _save_emergency(self, state: EmergencyState) -> None:
        await self.store.put_doc("policy_control", "emergency",
                                 state.model_dump(mode="json"))

    async def _emit(self, event_type: str, payload: dict, severity: str) -> None:
        if self.event_bus is None:
            return
        try:
            await self.event_bus.publish(event_type, payload, severity=severity,
                                         source="security")
        except Exception:  # noqa: BLE001
            pass