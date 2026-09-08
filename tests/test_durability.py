"""Durability round: DurableCheckpointer (restart survival) + CircuitBreaker.

Master spec §19 (long-horizon execution must survive restarts) and §9
(model routing must implement fallback and circuit breaking).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Durable checkpointer — a checkpoint written to the store is still readable
# after the checkpointer instance (and its process) is gone.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkpointer_survives_restart(svc):
    from agentos.orchestration.checkpoints import DurableCheckpointer

    # "process A" writes a checkpoint, then the object is discarded
    cp_a = DurableCheckpointer(svc.entity_store)
    config = {"configurable": {"thread_id": "mission-1", "checkpoint_ns": ""}}
    checkpoint = {
        "id": "cp-1", "v": 1, "ts": 1.0,
        "values": {"stage": "research", "objective": "Build ChurchApp",
                   "evidence": {"sources": ["a", "b"]}},
    }
    metadata = {"step": 3, "source": "research"}
    out = await cp_a.aput(config, checkpoint, metadata, {"x": 1})
    assert out["configurable"]["checkpoint_id"] == "cp-1"

    # "process B" — a brand-new checkpointer over the same store — resumes
    cp_b = DurableCheckpointer(svc.entity_store)
    resumed = await cp_b.aget_tuple(config)
    assert resumed is not None
    assert resumed.checkpoint["id"] == "cp-1"
    assert resumed.checkpoint["values"]["stage"] == "research"
    assert resumed.metadata["step"] == 3
    # the graph's run state survives exactly as written
    assert resumed.checkpoint["values"]["evidence"]["sources"] == ["a", "b"]

    # unknown thread -> None, not an error
    assert await cp_b.aget_tuple(
        {"configurable": {"thread_id": "never-ran"}}) is None


@pytest.mark.asyncio
async def test_checkpointer_tracks_latest_per_thread(svc):
    from agentos.orchestration.checkpoints import DurableCheckpointer

    cp = DurableCheckpointer(svc.entity_store)
    for i in range(3):
        await cp.aput(
            {"configurable": {"thread_id": "thread-x"}},
            {"id": f"cp-{i}", "values": {"step": i}}, {"step": i}, {})
    latest = await cp.aget_tuple({"configurable": {"thread_id": "thread-x"}})
    assert latest.checkpoint["id"] == "cp-2"
    assert latest.checkpoint["values"]["step"] == 2
    assert await cp.checkpoint_count() == 3
    threads = await cp.list_threads()
    assert any(t["thread_id"] == "thread-x" for t in threads)


# ---------------------------------------------------------------------------
# Circuit breaker — consecutive failures open the circuit, routing skips the
# model, a success closes it, cooldown lets it recover.
# ---------------------------------------------------------------------------

def test_circuit_breaker_opens_and_closes():
    from agentos.models.router import CircuitBreaker

    cb = CircuitBreaker(threshold=3, cooldown_seconds=60)
    assert not cb.is_open("deepseek-pro")
    cb.record_failure("deepseek-pro")
    cb.record_failure("deepseek-pro")
    assert not cb.is_open("deepseek-pro")
    cb.record_failure("deepseek-pro")
    assert cb.is_open("deepseek-pro")
    assert "deepseek-pro" in cb.summary()["open"]

    # unrelated models are unaffected
    assert not cb.is_open("gpt-5")

    # a success closes the circuit immediately
    cb.record_success("deepseek-pro")
    assert not cb.is_open("deepseek-pro")
    assert "deepseek-pro" not in cb.summary()["open"]


def test_circuit_breaker_cooldown_recovers():
    from agentos.models.router import CircuitBreaker

    cb = CircuitBreaker(threshold=1, cooldown_seconds=0.01)
    cb.record_failure("flaky")
    assert cb.is_open("flaky")
    import time
    time.sleep(0.05)
    # cooldown expired -> half-open, traffic allowed again
    assert not cb.is_open("flaky")
    # failure re-opens
    cb.record_failure("flaky")
    assert cb.is_open("flaky")


@pytest.mark.asyncio
async def test_router_routes_around_open_circuit(svc):
    """When the preferred model's circuit is open, failover picks a healthy
    fallback instead of returning the flaky model."""
    from agentos.config import Settings
    from agentos.orchestration.engine import OrchestratorEngine
    from agentos.services import Services
    import tempfile

    settings = Settings(database_url=None, redis_url=None,
                        workspace_dir=tempfile.mkdtemp() + "/ws",
                        log_level="WARNING")
    svc2 = Services(settings)
    await svc2.seed()
    try:
        svc2.engine = OrchestratorEngine(svc2)
        router = svc2.router
        agent = await svc2.agent_registry.get("ai-engineer")
        # pick the model the router would naturally choose
        model_id, _reason = await router.route(agent, description="implement feature")
        assert model_id, "echo provider must be routable"
        # a second, healthy model on the same (echo) provider so failover has
        # somewhere to route — this mirrors prod where multiple providers live
        from agentos.domain.models import ModelDef
        await svc2.model_registry.register(ModelDef(
            id="echo-b", provider="echo", name="echo-b", tier="t2",
            context_window=128000, price_in_per_million=0.0,
            price_out_per_million=0.0, capabilities=["offline"], fallbacks=[]))
        # fail the preferred model past the threshold -> circuit open
        for _ in range(router.breaker.threshold + 1):
            router.breaker.record_failure(model_id)
        assert router.breaker.is_open(model_id)
        assert not await router._available(model_id)
        # pick_for_provider (same tier) refuses the open model, takes healthy one
        picked = await router.pick_for_provider(
            (agent.model_policy or {}).get("tier", "t2"))
        assert picked is not None and picked != model_id
        # failover from the open model lands on the healthy alternative
        fb, reason = await router.failover(model_id, "timeout")
        assert fb is not None and fb != model_id
        assert "failover" in reason
        assert router.breaker.is_open(model_id)  # primary stays open
    finally:
        await svc2.close()