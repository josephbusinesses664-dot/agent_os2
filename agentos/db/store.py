"""Typed helpers over the generic document repository."""

from __future__ import annotations

from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from agentos.domain.models import (
    AgentDef,
    AgentEvaluation,
    AgentInstance,
    AgentMessage,
    ApiDef,
    ApprovalRequest,
    AuditEntry,
    Budget,
    DecisionRecord,
    EvaluationRecord,
    EvaluationRunSummary,
    Event,
    McpServer,
    MemoryEntry,
    MemoryLink,
    ModelDef,
    PerformanceStats,
    Project,
    Schedule,
    SkillDef,
    Task,
    ToolDef,
    TraceSpan,
    UsageRecord,
    WorkflowDef,
    WorkspaceRecord,
)

from .base import Repository

T = TypeVar("T", bound=BaseModel)

# Explicit storage key per model class. Field tuples join with ":".
KEY_FIELDS: dict[type, tuple[str, ...]] = {
    AgentDef: ("id",),
    AgentInstance: ("agent_id",),
    Task: ("task_id",),
    Project: ("project_id",),
    SkillDef: ("id",),
    ToolDef: ("name",),
    McpServer: ("name",),
    ModelDef: ("id",),
    ApiDef: ("api",),
    UsageRecord: ("usage_id",),
    Event: ("event_id",),
    AuditEntry: ("audit_id",),
    MemoryEntry: ("memory_id",),
    MemoryLink: ("link_id",),
    ApprovalRequest: ("approval_id",),
    AgentMessage: ("message_id",),
    DecisionRecord: ("decision_id",),
    AgentEvaluation: ("eval_id",),
    EvaluationRecord: ("eval_id",),
    EvaluationRunSummary: ("run_id",),
    TraceSpan: ("span_id",),
    PerformanceStats: ("agent_id", "window"),
    WorkflowDef: ("workflow_id",),
    Budget: ("scope", "scope_id"),
    WorkspaceRecord: ("workspace_id",),
    Schedule: ("schedule_id",),
}


class EntityStore:
    """Serializes pydantic models into the repository with zero boilerplate."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def save(self, collection: str, model: BaseModel) -> None:
        await self.repo.put(collection, self._key(model), model.model_dump(mode="json"))

    async def get(self, collection: str, key: str, model_type: Type[T]) -> Optional[T]:
        doc = await self.repo.get(collection, key)
        return model_type.model_validate(doc) if doc else None

    async def delete(self, collection: str, key: str) -> None:
        await self.repo.delete(collection, key)

    async def list(
        self, collection: str, model_type: Type[T], predicate: Optional[dict] = None
    ) -> list[T]:
        docs = await self.repo.query(collection, predicate)
        return [model_type.model_validate(d) for d in docs]

    async def list_docs(self, collection: str, predicate: Optional[dict] = None) -> list[dict]:
        """Raw documents (for telemetry/usage endpoints that don't need models)."""
        return await self.repo.query(collection, predicate)

    async def put_doc(self, collection: str, key: str, doc: dict) -> None:
        await self.repo.put(collection, key, doc)

    async def get_doc(self, collection: str, key: str) -> Optional[dict]:
        return await self.repo.get(collection, key)

    @staticmethod
    def _key(model: BaseModel) -> str:
        from enum import Enum

        fields = KEY_FIELDS.get(type(model))
        if fields is None:
            raise ValueError(f"no key fields defined for {type(model).__name__}")
        values = []
        for field in fields:
            value = getattr(model, field)
            values.append(value.value if isinstance(value, Enum) else str(value))
        return ":".join(values)