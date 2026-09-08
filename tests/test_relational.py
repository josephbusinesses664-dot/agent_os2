"""Relational schema (master spec §32: database).

Every aggregate lives in a dedicated table with a typed primary key and
foreign keys to the aggregates it references — enforced by the database, not
by application discipline. Runs against SQLite (aiosqlite) here; the same
schema (and the same `Repository` contract) is what the box runs on Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def _repo(tmp_path, name: str = "test.db"):
    from agentos.db.relational import RelationalRepository

    url = f"sqlite+aiosqlite:///{tmp_path / name}"
    return RelationalRepository(url)


# ---------------------------------------------------------------------------
# Typed keys + FK integrity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_typed_tables_and_fk_enforcement(tmp_path):
    repo = _repo(tmp_path)
    await repo.init()
    try:
        # project must exist before a task can reference it (FK enforced)
        await repo.put("projects", "proj_x", {"project_id": "proj_x", "name": "X"})
        await repo.put("tasks", "task_1",
                       {"task_id": "task_1", "project_id": "proj_x", "title": "hi"})
        assert (await repo.get("tasks", "task_1"))["title"] == "hi"

        # a task referencing a non-existent project is rejected by the DB
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await repo.put("tasks", "task_2",
                           {"task_id": "task_2", "project_id": "no-such-proj"})
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_typed_keys_split_roundtrip(tmp_path):
    """Composite keys ('scope:scope_id') map onto real columns, not string soup."""
    repo = _repo(tmp_path)
    await repo.init()
    try:
        await repo.put("budgets", "project:proj_x",
                       {"scope": "project", "scope_id": "proj_x",
                        "monthly_limit": 50})
        assert (await repo.get("budgets", "project:proj_x"))["monthly_limit"] == 50
        # a different scope id is a different row — key columns really typed
        await repo.put("budgets", "project:proj_y",
                       {"scope": "project", "scope_id": "proj_y",
                        "monthly_limit": 5})
        assert await repo.count("budgets") == 2
        assert (await repo.get("budgets", "project:proj_y"))["monthly_limit"] == 5
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_query_count_delete(tmp_path):
    repo = _repo(tmp_path)
    await repo.init()
    try:
        await repo.put("projects", "p1", {"project_id": "p1", "name": "one"})
        await repo.put("projects", "p2", {"project_id": "p2", "name": "two"})
        await repo.put("tasks", "t1", {"task_id": "t1", "project_id": "p1"})
        await repo.put("tasks", "t2", {"task_id": "t2", "project_id": "p1"})
        assert await repo.count("projects") == 2
        assert len(await repo.query("tasks")) == 2
        assert len(await repo.query("tasks", {"project_id": "p1"})) == 2
        assert len(await repo.query("tasks", {"project_id": "p2"})) == 0
        await repo.delete("tasks", "t1")
        assert await repo.get("tasks", "t1") is None
        assert await repo.count("tasks") == 1
        assert await repo.health() is True
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# Legacy migration: agentos_docs rows move into typed tables, idempotently,
# without bricking on orphaned rows.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrates_legacy_docs_rows(tmp_path):
    from sqlalchemy import insert as sa_insert

    from agentos.db.relational import DocRow

    now = datetime.now(timezone.utc)
    legacy = _repo(tmp_path)
    await legacy.init()
    async with legacy.session_factory() as session:
        rows = [
            ("projects", "p1", {"project_id": "p1", "name": "P"}),
            ("tasks", "task_old", {"task_id": "task_old", "title": "legacy",
                                   "project_id": "p1"}),
            ("tasks", "task_orphan", {"task_id": "task_orphan",
                                      "project_id": "missing"}),
            ("budgets", "global:global", {"scope": "global", "scope_id": "global",
                                          "monthly_limit": 10}),
            ("unknown_col", "x", {"v": 1}),  # no typed table -> stays legacy
        ]
        for collection, key, data in rows:
            await session.execute(sa_insert(DocRow).values(
                collection=collection, key=key, data=data, updated_at=now))
        await session.commit()
    await legacy.close()

    repo = _repo(tmp_path)
    await repo.init()
    migrated = await repo.migrate_from_docs()
    try:
        # valid rows land in the typed tables
        assert (await repo.get("tasks", "task_old"))["title"] == "legacy"
        assert (await repo.get("budgets", "global:global"))["monthly_limit"] == 10
        # orphaned rows (FK to a deleted parent) are skipped, not fatal
        assert await repo.get("tasks", "task_orphan") is None
        # collections without a typed table stay on the legacy store
        assert (await repo.get("unknown_col", "x"))["v"] == 1
        assert migrated >= 3
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# EntityStore integration — the whole platform works on the relational repo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entity_store_on_relational_repo(tmp_path):
    from agentos.db.relational import RelationalRepository
    from agentos.db.store import EntityStore
    from agentos.domain.models import Project

    repo = RelationalRepository(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
    await repo.init()
    store = EntityStore(repo)
    try:
        project = Project(project_id="prj_1", name="relational",
                          objective="test", user_id="u1")
        await store.save("projects", project)
        loaded = await store.get("projects", "prj_1", Project)
        assert loaded is not None and loaded.name == "relational"
        assert len(await store.list("projects", Project)) == 1
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# Model round-trip on every mapped collection (schema completeness)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_schema_collections_roundtrip(tmp_path):
    from agentos.db.relational import SCHEMA
    from agentos.domain.models import (
        AgentDef, AgentInstance, ApprovalRequest, AuditEntry, Budget,
        DecisionRecord, Event, MemoryEntry, MemoryLink, Project, SkillDef,
        Task, ToolDef, UsageRecord, WorkspaceRecord,
    )

    repo = _repo(tmp_path)
    await repo.init()
    store = __import__("agentos.db.store", fromlist=["EntityStore"]).EntityStore(repo)
    try:
        # FK prerequisites first (project/agent must exist before refs)
        agent = AgentDef(id="worker-1", role="worker", name="w", department="eng")
        project = Project(project_id="prj_r", name="r", objective="o", user_id="u")
        await store.save("agents", agent)
        await store.save("projects", project)

        samples = [
            ("tasks", Task(task_id="t_r", project_id="prj_r", title="t")),
            ("agent_instances", AgentInstance(agent_id="worker-1")),
            ("events", Event(event_id="e_r", project_id="prj_r", type="test")),
            ("audit", AuditEntry(audit_id="a_r", actor="worker-1",
                                 action="test", project_id="prj_r")),
            ("approvals", ApprovalRequest(approval_id="ap_r", agent_id="worker-1",
                                          action="deploy", project_id="prj_r")),
            ("memory", MemoryEntry(memory_id="m_r", scope="org", owner_id="org",
                                   content="fact")),
            ("memory_links", MemoryLink(link_id="ml_r", source_id="m_r",
                                         target_id="m_r2")),
            ("budgets", Budget(scope="project", scope_id="prj_r")),
            ("usage", UsageRecord(usage_id="u_r", model="echo", provider="echo")),
            ("skills", SkillDef(id="s_r", name="skill", description="d",
                                 category="cat")),
            ("tools", ToolDef(name="tool_r", description="tool")),
            ("workspaces", WorkspaceRecord(workspace_id="ws_r",
                                           project_id="prj_r")),
        ]
        for collection, model in samples:
            await store.save(collection, model)
        # every mapped collection is readable back
        assert await store.get("tasks", "t_r", Task) is not None
        assert await store.get("workspaces", "ws_r", WorkspaceRecord) is not None
        assert await store.get("budgets", "project:prj_r", Budget) is not None
        assert len(SCHEMA) >= 25, "schema registry should cover the aggregates"
    finally:
        await repo.close()