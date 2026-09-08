"""Model Router.

Routes a task to a model based on task complexity, risk, importance,
required reasoning, agent role policy, budget state and provider availability.
Policies are defaults, not hardcoded rules — swap tiers/models freely.

Decision flow:
    agent policy tier
        → complexity adjustment (up, bounded by agent max_tier)
        → budget check (approve | downgrade | reject)
        → availability + fallback chain
"""

from __future__ import annotations

import re
import time
from typing import Optional

from agentos.budgets.manager import BudgetManager
from agentos.config import Settings
from agentos.domain.models import AgentDef, BudgetScope, Task
from agentos.registries.model_registry import ModelRegistry

HIGH_COMPLEXITY = {
    "architecture", "strategy", "security", "debug", "review", "design",
    "migration", "refactor", "research", "plan", "roadmap", "prd", "threat",
}
MEDIUM_COMPLEXITY = {"implement", "build", "feature", "test", "integration", "api"}

# Task-risk vocabulary (Phase 8: risk-aware routing). Risky tasks justify
# stronger reasoning regardless of raw length/keyword complexity.
HIGH_RISK_TASK = {
    "production", "deploy", "deployment", "migration", "irreversible",
    "security", "credentials", "secret", "payment", "billing", "delete",
    "destructive", "database", "schema change", "rollback",
}

# Agent risk tolerance shifts the complexity bar: minimal-tolerance agents
# (devops, security, QA) get stronger models sooner because their domains
# are exactly the ones where mistakes are expensive. High-tolerance agents
# (executive, growth experiments) tolerate cheaper models on ambiguity.
TOLERANCE_COMPLEXITY_ADJUST = {"minimal": -1, "low": 0, "moderate": 0, "high": 1}

TIER_ORDER = ["t0", "t1", "t2", "t3"]


def _bump_tier(tier: str) -> str:
    if tier in TIER_ORDER and tier != "t3":
        return TIER_ORDER[TIER_ORDER.index(tier) + 1]
    return tier


def _drop_tier(tier: str) -> str:
    if tier in TIER_ORDER and tier != "t0":
        return TIER_ORDER[TIER_ORDER.index(tier) - 1]
    return tier


def complexity_score(text: str) -> int:
    words = set(re.findall(r"[a-z]+", text.lower()))
    high = len(words & HIGH_COMPLEXITY)
    medium = len(words & MEDIUM_COMPLEXITY)
    length_bonus = 1 if len(text) > 600 else 0
    return high * 2 + medium + length_bonus


class CircuitBreaker:
    """Consecutive-failure circuit breaker per model id.

    After `threshold` consecutive failures a model's circuit opens and it is
    routed around for `cooldown_seconds`; a success closes (resets) the
    circuit. This is availability state, not a judgment on the model — it
    exists so one flaky provider cannot stall the whole organization.
    """

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 120.0) -> None:
        self.threshold = max(1, threshold)
        self.cooldown = cooldown_seconds
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def record_failure(self, model_id: str) -> None:
        count = self._failures.get(model_id, 0) + 1
        self._failures[model_id] = count
        if count >= self.threshold:
            self._open_until[model_id] = time.monotonic() + self.cooldown

    def record_success(self, model_id: str) -> None:
        self._failures.pop(model_id, None)
        self._open_until.pop(model_id, None)

    def is_open(self, model_id: str) -> bool:
        until = self._open_until.get(model_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            # cooldown expired — half-open: allow traffic, a failure re-opens
            self._open_until.pop(model_id, None)
            self._failures.pop(model_id, None)
            return False
        return True

    def summary(self) -> dict:
        now = time.monotonic()
        return {
            "threshold": self.threshold,
            "cooldown_seconds": self.cooldown,
            "open": sorted(
                m for m, until in self._open_until.items()
                if now < until),
            "consecutive_failures": dict(self._failures),
        }


class ModelRouter:
    def __init__(self, settings: Settings, model_registry: ModelRegistry,
                 budgets: BudgetManager, providers: dict,
                 performance: Any = None) -> None:
        self.settings = settings
        self.registry = model_registry
        self.budgets = budgets
        self.providers = providers
        self.performance = performance  # PerformanceTracker | None
        self.breaker = CircuitBreaker(
            threshold=getattr(settings, "model_circuit_threshold", 3),
            cooldown_seconds=getattr(settings, "model_circuit_cooldown_seconds", 120.0),
        )

    # -- availability -------------------------------------------------------
    async def _available(self, model_id: str) -> bool:
        if self.breaker.is_open(model_id):
            return False
        model = await self.registry.get(model_id)
        if not model or not model.enabled:
            return False
        provider = self.providers.get(model.provider)
        if provider is None:
            return False
        return provider.is_configured()

    def record_success(self, model_id: str) -> None:
        self.breaker.record_success(model_id)

    async def pick_for_provider(self, tier: str, excluded: set[str] | None = None) -> Optional[str]:
        models = await self.registry.by_tier(tier)
        models.sort(key=lambda m: m.price_in_per_million + m.price_out_per_million)
        for model in models:
            if excluded and model.id in excluded:
                continue
            if await self._available(model.id):
                return model.id
        return None

    async def estimate_cost(self, model_id: str, est_tokens: int = 2000) -> float:
        model = await self.registry.get(model_id)
        if not model:
            return 0.0
        # assume ~60/40 prompt/completion split for estimates
        prompt = int(est_tokens * 0.6)
        completion = est_tokens - prompt
        return (prompt / 1e6 * model.price_in_per_million
                + completion / 1e6 * model.price_out_per_million)

    # -- routing ------------------------------------------------------------
    async def route(self, agent: AgentDef, task: Task | None = None,
                    description: str = "", project_id: Optional[str] = None,
                    force_model: Optional[str] = None) -> tuple[Optional[str], str]:
        """Return (model_id, reason). model_id None means 'no model (rejected)'."""
        if force_model:
            # forced models must still exist and be enabled: a caller may pin
            # a model, but may not invent one (no phantom-model execution).
            forced = await self.registry.get(force_model)
            if forced is None or not forced.enabled:
                return None, f"forced model unknown or disabled: {force_model}"
            return force_model, "forced by caller"
        policy = agent.model_policy or {}
        base_tier = policy.get("tier", "t2")
        max_tier = policy.get("max_tier", "t3")
        text = description or (task.description if task else "") or agent.role
        score = complexity_score(text)
        tier = base_tier
        if score >= 6 and tier in ("t1", "t2"):
            tier = "t2" if tier == "t1" else "t3"
        tier = min(tier, max_tier) if max_tier in ("t1", "t2", "t3") else tier

        # risk-aware routing (Phase 8): the task's own risk vocabulary and
        # the agent's risk tolerance are selection factors, not just prose.
        # A risky task handled by a minimal-risk-tolerance agent earns a
        # stronger model sooner; the same task with a high-tolerance agent
        # does not automatically escalate. Explainable, bounded by max_tier.
        risk_note = ""
        task_risk = len(HIGH_RISK_TASK & set(re.findall(r"[a-z]+", text.lower())))
        if task_risk and agent.identity is not None:
            tolerance = agent.identity.risk_tolerance
            adjust = TOLERANCE_COMPLEXITY_ADJUST.get(tolerance, 0)
            if (task_risk >= 1 and adjust < 0) or (task_risk >= 2 and adjust > 0):
                effective = score + task_risk + adjust
                if effective >= 5 and tier in ("t1", "t2") and tier < max_tier:
                    tier = "t2" if tier == "t1" else "t3"
                    risk_note = (f" | risk-aware: {task_risk} risk signals, "
                                 f"tolerance {tolerance} → tier {tier}")

        # performance influence: track record shifts routing (operational, not
        # cosmetic) — weak performers go to stronger models, proven ones can
        # drop to cheaper tiers.
        perf_note = ""
        if self.performance is not None:
            try:
                influence = await self.performance.influence(agent.id)
                bump = influence.get("tier_bump", 0)
                if bump > 0 and tier < max_tier:
                    tier = _bump_tier(tier)
                    perf_note = f" | perf bump: {influence.get('reason', '')}"
                elif bump < 0:
                    tier = _drop_tier(tier)
                    perf_note = f" | perf downgrade: {influence.get('reason', '')}"
            except Exception:  # noqa: BLE001
                pass

        preferred = [m for m in policy.get("preferred_models", []) if await self._available(m)]
        if preferred:
            model_id = preferred[0]
        else:
            model_id = await self.pick_for_provider(tier)
        if not model_id:
            # fall back to a cheaper tier rather than failing
            for fallback_tier in ("t2", "t1"):
                model_id = await self.pick_for_provider(fallback_tier)
                if model_id:
                    break
        if not model_id:
            return None, "no configured model available"

        est_tokens = min(2000 + score * 400, 12000)
        est_cost = await self.estimate_cost(model_id, est_tokens)
        scope = BudgetScope.PROJECT if project_id else BudgetScope.GLOBAL
        decision = await self.budgets.check(scope, project_id or "global", est_cost, suggested_tier=tier)
        if decision.action == "reject":
            return None, decision.reason
        if decision.action == "downgrade" and decision.suggested_model:
            return decision.suggested_model, decision.reason + " → " + decision.suggested_model
        reason = (f"tier {tier} (complexity {score}) via {agent.id} policy, "
                  f"est ${est_cost:.4f} — {decision.reason}{perf_note}{risk_note}")
        return model_id, reason

    async def failover(self, primary: str, error: str) -> tuple[Optional[str], str]:
        """Choose a fallback when `primary` fails. Returns (model_id, reason)."""
        self.breaker.record_failure(primary)
        model = await self.registry.get(primary)
        if not model:
            return await self.pick_for_provider("t2"), "primary unknown"
        tried = {primary}
        for fallback in self.registry.fallback_chain(model):
            if fallback in tried:
                continue
            tried.add(fallback)
            if await self._available(fallback):
                return fallback, f"failover from {primary}: {error[:120]}"
        # last resort: cheapest configured model
        for tier in ("t2", "t1"):
            for m in await self.registry.by_tier(tier):
                if await self._available(m.id) and m.id not in tried:
                    return m.id, f"emergency failover (tier {tier})"
        return None, "no fallback model available"