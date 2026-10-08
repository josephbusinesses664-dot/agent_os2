"""Budget manager + model router tests."""

import pytest

from agentos.budgets.manager import BudgetManager
from agentos.config import Settings
from agentos.db.memory import MemoryRepository
from agentos.db.store import EntityStore
from agentos.domain.models import (
    Budget,
    BudgetScope,
    ModelDef,
    Task,
    UsageRecord,
)


async def _fake_pick(tier: str) -> str:
    return "deepseek-flash"


def make_manager(monthly=10.0, daily=5.0, auto_downgrade=True) -> BudgetManager:
    settings = Settings(global_budget_monthly=monthly, global_budget_daily=daily,
                        auto_downgrade_on_budget=auto_downgrade,
                        workspace_dir="/tmp/agentos-test")
    store = EntityStore(MemoryRepository())
    return BudgetManager(settings, store, downgrade_picker=_fake_pick)


@pytest.mark.asyncio
async def test_budget_approve_within_limits():
    mgr = make_manager(monthly=10.0)
    decision = await mgr.check(BudgetScope.GLOBAL, "global", 0.5)
    assert decision.allowed and decision.action == "approve"


@pytest.mark.asyncio
async def test_budget_reject_when_exhausted():
    mgr = make_manager(monthly=1.0, daily=0.5, auto_downgrade=False)
    await mgr.record(UsageRecord(provider="p", model="m", estimated_cost=0.9))
    decision = await mgr.check(BudgetScope.GLOBAL, "global", 0.5)
    assert not decision.allowed and decision.action == "reject"


@pytest.mark.asyncio
async def test_budget_downgrade():
    mgr = make_manager(monthly=0.5, daily=0.2, auto_downgrade=True)
    await mgr.record(UsageRecord(provider="p", model="m", estimated_cost=0.4))
    decision = await mgr.check(BudgetScope.GLOBAL, "global", 0.3, suggested_tier="t2")
    assert decision.action == "downgrade"
    assert decision.suggested_model is not None


@pytest.mark.asyncio
async def test_budget_tracks_project_and_global():
    mgr = make_manager()
    await mgr.record(UsageRecord(provider="p", model="m", estimated_cost=1.25,
                                 project_id="prj1", agent_id="cto", task_id="t1"))
    global_budget = await mgr.get(BudgetScope.GLOBAL, "global")
    project_budget = await mgr.get(BudgetScope.PROJECT, "prj1")
    assert global_budget.spent_month == pytest.approx(1.25)
    assert project_budget.spent_month == pytest.approx(1.25)
    assert await mgr.project_cost("prj1") == pytest.approx(1.25)


@pytest.mark.asyncio
async def test_budget_day_rollover():
    mgr = make_manager()
    budget = await mgr.get(BudgetScope.GLOBAL, "global")
    budget.spent_day = 3.0
    budget.day_bucket = "2000-01-01"  # force stale bucket
    await mgr.save(budget)
    refreshed = await mgr.get(BudgetScope.GLOBAL, "global")
    assert refreshed.spent_day == 0.0
    assert refreshed.day_bucket != "2000-01-01"


@pytest.mark.asyncio
async def test_router_tier_routing(svc):
    agent = await svc.agent_registry.get("community-researcher")
    task = Task(task_id="t1", project_id="p1",
                title="Deep competitive research with architectural analysis",
                description="Research competitor moats, architecture and strategy for a SaaS product.")
    model_id, reason = await svc.router.route(agent, task)
    assert model_id is not None
    # complexity should push to t2+ (community-researcher policy is t2)
    assert "tier t2" in reason or "tier t3" in reason


@pytest.mark.asyncio
async def test_router_cheap_for_routine(svc):
    agent = await svc.agent_registry.get("seo-agent")
    task = Task(task_id="t2", project_id="p1", title="Summarize this list",
                description="Classify and summarize 10 short texts.")
    model_id, reason = await svc.router.route(agent, task)
    assert model_id is not None
    assert "tier t2" in reason


@pytest.mark.asyncio
async def test_router_failover_chain(svc):
    # echo is the only configured provider; failover must degrade gracefully
    model, reason = await svc.router.failover("nonexistent-model", "boom: provider outage")
    # primary unknown fallback lands on a t2 model (which is echo in tests)
    assert model == "echo"
    assert "primary unknown" in reason


@pytest.mark.asyncio
async def test_router_prefers_available_echo_offline(svc):
    agent = await svc.agent_registry.get("test-engineer")
    task = Task(task_id="t3", project_id="p1", title="Run the unit tests",
                description="Run tests and report output.")
    model_id, _ = await svc.router.route(agent, task)
    # offline mode must still route (to echo)
    assert model_id is not None
    assert model_id == "echo" or model_id in {m.id for m in await svc.model_registry.list()}