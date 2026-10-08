"""Model Registry.

Every model is a ModelDef: provider, tier (t0 deterministic … t3 executive),
pricing per 1M tokens, context window and fallbacks. Defaults below are the
starting policy — nothing is hardcoded into the router itself; any model can
be added, disabled or re-tiered.
"""

from __future__ import annotations

from typing import Optional

from agentos.db.store import EntityStore
from agentos.domain.models import ModelDef


def default_models() -> list[ModelDef]:
    return [
        # --- Tier 3: executive reasoning -----------------------------------
        ModelDef(id="claude-sonnet-4-5", provider="anthropic", name="claude-sonnet-4-5",
                 tier="t3", context_window=200000,
                 price_in_per_million=3.0, price_out_per_million=15.0,
                 capabilities=["reasoning", "coding", "analysis", "tool-use"],
                 fallbacks=["deepseek-pro", "glm-pro"]),
        # --- Tier 2: senior workers ----------------------------------------
        ModelDef(id="deepseek-pro", provider="deepseek", name="deepseek-v4-pro",
                 tier="t2", context_window=128000,
                 price_in_per_million=0.27, price_out_per_million=1.10,
                 capabilities=["coding", "analysis", "research"],
                 fallbacks=["glm-pro", "claude-sonnet-4-5"]),
        ModelDef(id="glm-pro", provider="glm", name="glm-4-plus",
                 tier="t2", context_window=128000,
                 price_in_per_million=0.5, price_out_per_million=2.0,
                 capabilities=["coding", "analysis"],
                 fallbacks=["deepseek-pro"]),
        # --- Tier 1: cheap workers -----------------------------------------
        ModelDef(id="deepseek-flash", provider="deepseek", name="deepseek-v4-flash",
                 tier="t1", context_window=128000,
                 price_in_per_million=0.07, price_out_per_million=0.28,
                 capabilities=["classification", "extraction", "summarization"],
                 fallbacks=["glm-flash", "local"]),
        ModelDef(id="glm-flash", provider="glm", name="glm-4-flash",
                 tier="t1", context_window=128000,
                 price_in_per_million=0.1, price_out_per_million=0.3,
                 capabilities=["classification", "summarization"],
                 fallbacks=["deepseek-flash"]),
        ModelDef(id="local", provider="openai_compat", name="local-model",
                 tier="t1", context_window=32000,
                 price_in_per_million=0.0, price_out_per_million=0.0,
                 capabilities=["classification", "summarization"],
                 fallbacks=["deepseek-flash"]),
        # --- Tier 0: deterministic / no LLM (used when no model is needed) --
        ModelDef(id="deterministic", provider="echo", name="deterministic",
                 tier="t0", context_window=0,
                 price_in_per_million=0.0, price_out_per_million=0.0,
                 capabilities=["deterministic"], fallbacks=[]),
        # --- Offline / testing ---------------------------------------------
        ModelDef(id="echo", provider="echo", name="echo",
                 tier="t2", context_window=128000,
                 price_in_per_million=0.0, price_out_per_million=0.0,
                 capabilities=["offline"], fallbacks=[]),
    ]


class ModelRegistry:
    def __init__(self, store: EntityStore) -> None:
        self.store = store
        self._collection = "models"

    async def seed_defaults(self) -> int:
        existing = await self.list()
        if existing:
            return len(existing)
        for model in default_models():
            await self.store.save(self._collection, model)
        return len(default_models())

    async def list(self, enabled_only: bool = True) -> list[ModelDef]:
        models = await self.store.list(self._collection, ModelDef)
        models.sort(key=lambda m: (m.tier, m.id))
        return [m for m in models if m.enabled or not enabled_only]

    async def get(self, model_id: str) -> Optional[ModelDef]:
        return await self.store.get(self._collection, model_id, ModelDef)

    async def by_tier(self, tier: str) -> list[ModelDef]:
        return [m for m in await self.list() if m.tier == tier]

    async def register(self, model: ModelDef) -> ModelDef:
        await self.store.save(self._collection, model)
        return model

    async def set_enabled(self, model_id: str, enabled: bool) -> ModelDef:
        model = await self.get(model_id)
        if not model:
            raise KeyError(f"model {model_id} not found")
        model.enabled = enabled
        await self.store.save(self._collection, model)
        return model

    def fallback_chain(self, model: ModelDef) -> list[str]:
        chain = list(model.fallbacks)
        return chain