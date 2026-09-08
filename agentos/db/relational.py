"""Relational PostgreSQL schema (master spec §32: database).

The org's state is stored *relationally*, not as opaque JSON blobs: every
aggregate gets a dedicated table with a typed primary key and foreign keys to
the aggregates it references. Cross-aggregate integrity (a task's project
must exist, an approval's task must exist, ...) is enforced by the database.

The `Repository` interface is preserved so no business code changes — the
relational repository speaks the same put/get/query contract, but maps each
collection onto its own table. An untyped fallback table (`agentos_docs`)
remains for collections without a dedicated table, and legacy rows are
migrated into the new tables on init.

Tables (per collection):
    agents(id)                     agent_instances(agent_id → agents.id)
    projects(project_id)           tasks(task_id, project_id → projects.id,
                                          assigned_agent → agents.id)
    skills(id)  tools(name)  mcp(name)  models(id)  apis(api)
    events(event_id, project_id → projects.id, agent_id → agents.id)
    audit(audit_id, project_id → projects.id, task_id → tasks.id,
          agent_id → agents.id)
    memory(memory_id, owner_id)    memory_links(link_id)
    approvals(approval_id, project_id → projects.id, task_id → tasks.id,
              agent_id → agents.id)
    budgets(scope, scope_id)       messages(message_id, project_id → projects.id)
    decisions(decision_id, project_id → projects.id)
    evaluations(eval_id)  eval_runs(run_id)  performance(agent_id, window)
    traces(span_id)  workflows(workflow_id)  workspaces(workspace_id,
              project_id → projects.id, task_id → tasks.id, agent_id → agents.id)
    checkpoints(thread_id, ns, checkpoint_id)  checkpoint_latest(thread_id)
    policy_control(key)

Design notes:
- Every table keeps the full aggregate payload in a JSONB `data` column so
  schema evolution is cheap; the *keys* and *references* are typed columns
  with real FK constraints. This is the standard hybrid: relational identity
  and integrity at the database, flexible payloads alongside.
- `ON DELETE RESTRICT` (the default) means the database refuses to delete a
  project that still has tasks — integrity wins over silent orphaning.
- Legacy `agentos_docs` rows are migrated idempotently on `init()`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, and_, func, \
    insert, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import Column

from .base import Repository

logger = logging.getLogger("agentos.db.relational")

# Table/column builder alias used in _build_tables
from sqlalchemy import Table  # noqa: E402

# JSONB in Postgres, plain JSON elsewhere (SQLite tests) — same SQLAlchemy
# column, dialect-aware type.
_json_type = JSONB().with_variant(JSON(), "sqlite")

# Collection -> table name + key column names (in key order) + FK map:
#   {column_name: (ref_table, ref_column)}
# Collections absent from this map fall back to the legacy `agentos_docs`
# table (e.g. arbitrary doc-store usage that predates the schema).
SCHEMA: dict[str, dict[str, Any]] = {
    "agents": {"table": "agents", "keys": ["id"]},
    "agent_instances": {"table": "agent_instances", "keys": ["agent_id"],
                        "fks": {"agent_id": ("agents", "id")}},
    "projects": {"table": "projects", "keys": ["project_id"]},
    "tasks": {"table": "tasks", "keys": ["task_id"],
              "fks": {"project_id": ("projects", "project_id"),
                      "assigned_agent": ("agents", "id")}},
    "skills": {"table": "skills", "keys": ["id"]},
    "tools": {"table": "tools", "keys": ["name"]},
    "mcp": {"table": "mcp", "keys": ["name"]},
    "models": {"table": "models", "keys": ["id"]},
    "apis": {"table": "apis", "keys": ["api"]},
    "events": {"table": "events", "keys": ["event_id"],
               "fks": {"project_id": ("projects", "project_id"),
                       "agent_id": ("agents", "id")}},
    "audit": {"table": "audit", "keys": ["audit_id"],
              "fks": {"project_id": ("projects", "project_id"),
                      "task_id": ("tasks", "task_id"),
                      "agent_id": ("agents", "id")}},
    "memory": {"table": "memory", "keys": ["memory_id"]},
    "memory_links": {"table": "memory_links", "keys": ["link_id"]},
    "approvals": {"table": "approvals", "keys": ["approval_id"],
                  "fks": {"project_id": ("projects", "project_id"),
                          "task_id": ("tasks", "task_id"),
                          "agent_id": ("agents", "id")}},
    "budgets": {"table": "budgets", "keys": ["scope", "scope_id"]},
    "messages": {"table": "messages", "keys": ["message_id"],
                 "fks": {"project_id": ("projects", "project_id")}},
    "decisions": {"table": "decisions", "keys": ["decision_id"],
                  "fks": {"project_id": ("projects", "project_id")}},
    "evaluations": {"table": "evaluations", "keys": ["eval_id"]},
    "eval_runs": {"table": "eval_runs", "keys": ["run_id"]},
    "performance": {"table": "performance", "keys": ["agent_id", "window"],
                    "fks": {"agent_id": ("agents", "id")}},
    "traces": {"table": "traces", "keys": ["span_id"]},
    "workflows": {"table": "workflows", "keys": ["workflow_id"]},
    "workspaces": {"table": "workspaces", "keys": ["workspace_id"],
                   "fks": {"project_id": ("projects", "project_id"),
                           "task_id": ("tasks", "task_id"),
                           "agent_id": ("agents", "id")}},
    "checkpoints": {"table": "checkpoints", "keys": ["thread_id", "checkpoint_ns", "checkpoint_id"]},
    "checkpoint_latest": {"table": "checkpoint_latest", "keys": ["thread_id"]},
    "policy_control": {"table": "policy_control", "keys": ["key"]},
}


class RelationalBase(DeclarativeBase):
    pass


def _build_tables() -> None:
    """Declare one table per collection in SCHEMA (idempotent)."""
    for collection, spec in SCHEMA.items():
        table_name = spec["table"]
        keys = spec["keys"]
        fks = spec.get("fks", {})
        columns: list[Column] = []
        seen: set[str] = set()
        for col in keys + list(fks.keys()):
            if col in seen:
                continue
            seen.add(col)
            ref = fks.get(col)
            if ref:
                ref_table, ref_col = ref
                columns.append(Column(col, String(160),
                                      ForeignKey(f"{ref_table}.{ref_col}",
                                                 ondelete="RESTRICT"),
                                      primary_key=(col in keys)))
            else:
                columns.append(Column(col, String(160), primary_key=(col in keys)))
        columns.append(Column("data", _json_type, nullable=False))
        columns.append(Column("updated_at", DateTime(timezone=True), nullable=False))
        table = Table(table_name, RelationalBase.metadata, *columns)
        # index on every FK column (they are join hot-spots)
        for col in fks:
            if col not in keys:
                Index(f"ix_{table_name}_{col}", table.c[col])


_build_tables()


# Legacy untyped store (kept for collections not in SCHEMA + migration source).
class DocRow(RelationalBase):
    __tablename__ = "agentos_docs"
    collection = Column(String(64), primary_key=True)
    key = Column(String(256), primary_key=True)
    data = Column(_json_type, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class RelationalRepository(Repository):
    """`Repository` implemented on the relational schema above.

    put/get/query preserve the document contract; keys are typed columns and
    references are real FKs, so referential integrity is database-enforced.
    Works on PostgreSQL (asyncpg) and SQLite (aiosqlite) — the latter for
    offline tests.
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_async_engine(database_url, **kwargs)
        if database_url.startswith("sqlite"):
            # SQLite only enforces FKs when the pragma is set per-connection
            from sqlalchemy import event

            @event.listens_for(self.engine.sync_engine, "connect")
            def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    # -- lifecycle --------------------------------------------------------
    async def init(self) -> None:
        # create_all + migration must be serialized: on the box the app and
        # the worker containers boot together and would otherwise race
        # CREATE TABLE (one wins, the other crashes on the implicit type
        # index). A Postgres advisory lock arbitrates across processes;
        # SQLite (tests) is single-process so no lock is needed.
        if self.engine.dialect.name == "postgresql":
            async with self.engine.begin() as conn:
                await conn.execute(text("SELECT pg_advisory_lock(727001)"))
                try:
                    await conn.run_sync(RelationalBase.metadata.create_all)
                finally:
                    await conn.execute(text("SELECT pg_advisory_unlock(727001)"))
        else:
            async with self.engine.begin() as conn:
                await conn.run_sync(RelationalBase.metadata.create_all)
        await self.migrate_from_docs()

    async def close(self) -> None:
        await self.engine.dispose()

    async def migrate_from_docs(self) -> int:
        """Idempotently copy legacy agentos_docs rows into their typed tables.

        Runs on every init — `INSERT ... ON CONFLICT DO NOTHING` semantics
        make re-runs harmless. Returns the number of rows migrated.

        Row order matters: FK-referenced rows must land before the rows that
        reference them (a project before its events, an agent before its
        approvals), so collections are migrated in dependency order — parents
        first. Truly orphaned rows (references to parents that never existed)
        are logged and skipped without bricking the migration.
        """
        migrated = 0
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(DocRow))).scalars().all()
            # group by collection, then order collections: parents before
            # children (a collection with FK targets migrates after them)
            by_collection: dict[str, list] = {}
            for row in rows:
                by_collection.setdefault(row.collection, []).append(row)
            for collection in _migration_order():
                for row in by_collection.get(collection, []):
                    spec = SCHEMA.get(row.collection)
                    if not spec:
                        continue  # stays in the legacy table
                    values = _parse_key(row.key, spec["keys"])
                    if values is None:
                        continue
                    try:
                        # savepoint per row: a failing insert (orphaned FK)
                        # rolls back to the savepoint and leaves the migration
                        # usable
                        async with session.begin_nested():
                            migrated += await self._upsert_locked(
                                session, row.collection, values, row.data,
                                row.updated_at)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "skipping unmigratable legacy %s:%s — %s",
                            row.collection, row.key, exc)
            await session.commit()
        if migrated:
            logger.info("migrated %d legacy rows into relational tables", migrated)
        return migrated

    # -- contract ----------------------------------------------------------
    async def put(self, collection: str, key: str, doc: dict) -> None:
        spec = SCHEMA.get(collection)
        if not spec:
            await self._put_legacy(collection, key, doc)
            return
        values = _parse_key(key, spec["keys"])
        if values is None:
            raise ValueError(f"cannot parse key {key!r} for collection {collection}")
        async with self.session_factory() as session:
            await self._upsert_locked(session, collection, values, doc)
            await session.commit()

    async def get(self, collection: str, key: str) -> Optional[dict]:
        spec = SCHEMA.get(collection)
        if not spec:
            async with self.session_factory() as session:
                row = await session.get(DocRow, (collection, key))
                return dict(row.data) if row else None
        values = _parse_key(key, spec["keys"])
        if values is None:
            return None
        async with self.session_factory() as session:
            row = await self._get_row(session, collection, values)
            return dict(row) if row else None

    async def delete(self, collection: str, key: str) -> None:
        spec = SCHEMA.get(collection)
        async with self.session_factory() as session:
            if not spec:
                row = await session.get(DocRow, (collection, key))
                if row:
                    await session.delete(row)
            else:
                values = _parse_key(key, spec["keys"])
                if values is None:
                    return
                from sqlalchemy import delete as sa_delete

                table = RelationalBase.metadata.tables[spec["table"]]
                cond = and_(*[table.c[k] == v for k, v in values.items()])
                await session.execute(sa_delete(table).where(cond))
            await session.commit()

    async def query(self, collection: str, predicate: Optional[dict] = None) -> list[dict]:
        predicate = predicate or {}
        spec = SCHEMA.get(collection)
        docs = []
        async with self.session_factory() as session:
            if not spec:
                rows = (await session.execute(
                    select(DocRow).where(DocRow.collection == collection))).scalars().all()
                docs = [dict(r.data) for r in rows]
            else:
                table = RelationalBase.metadata.tables[spec["table"]]
                stmt = select(table.c.data)
                rows = (await session.execute(stmt)).scalars().all()
                docs = [dict(r) for r in rows]
        if predicate:
            docs = [d for d in docs if all(d.get(k) == v for k, v in predicate.items())]
        return docs

    async def count(self, collection: str, predicate: Optional[dict] = None) -> int:
        return len(await self.query(collection, predicate))

    async def health(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- internals ---------------------------------------------------------
    async def _put_legacy(self, collection: str, key: str, doc: dict) -> None:
        async with self.session_factory() as session:
            row = await session.get(DocRow, (collection, key))
            if row is None:
                session.add(DocRow(collection=collection, key=key, data=doc,
                                   updated_at=func.now()))
            else:
                row.data = doc
                row.updated_at = func.now()
            await session.commit()

    async def _get_row(self, session: AsyncSession, collection: str,
                       values: dict[str, str]) -> Any:
        table = RelationalBase.metadata.tables[SCHEMA[collection]["table"]]
        cond = and_(*[table.c[k] == v for k, v in values.items()])
        stmt = select(table.c.data).where(cond)
        row = (await session.execute(stmt)).scalar_one_or_none()
        return row  # the JSON payload dict itself

    async def _upsert_locked(self, session: AsyncSession, collection: str,
                             values: dict[str, str], doc: dict,
                             updated_at: Optional[Any] = None) -> int:
        """Upsert a row into the typed table. ON CONFLICT DO NOTHING for
        migration (existing rows win); DO UPDATE for live writes."""
        spec = SCHEMA[collection]
        table = RelationalBase.metadata.tables[spec["table"]]
        fk_columns = spec.get("fks", {})
        insert_values = dict(values)
        for col in fk_columns:
            # optional references default to "" in several models — treat
            # empty as NULL so the FK is not matched against a phantom row
            ref = doc.get(col)
            insert_values[col] = ref if ref not in (None, "") else None
        insert_values["data"] = doc
        insert_values["updated_at"] = updated_at if updated_at else func.now()
        dialect = self.engine.dialect.name
        base = pg_insert if dialect == "postgresql" else sqlite_insert
        stmt = base(table).values(**insert_values)
        if updated_at is not None:
            # migration: existing rows win (idempotent backfill)
            stmt = stmt.on_conflict_do_nothing(index_elements=spec["keys"])
        else:
            stmt = stmt.on_conflict_do_update(
                index_elements=spec["keys"],
                set_={"data": doc, "updated_at": func.now(), **{
                    col: (doc.get(col) if doc.get(col) not in (None, "") else None)
                    for col in fk_columns}})
        await session.execute(stmt)
        return 1


def _parse_key(key: str, key_columns: list[str]) -> Optional[dict[str, str]]:
    """Split a ':'-joined storage key back into typed column values."""
    parts = key.split(":")
    if len(parts) != len(key_columns):
        return None
    return dict(zip(key_columns, parts))


def _migration_order() -> list[str]:
    """Collections ordered parents-first by their FK targets."""
    targets: dict[str, set[str]] = {c: set() for c in SCHEMA}
    for collection, spec in SCHEMA.items():
        for _col, (ref_table, _ref_col) in spec.get("fks", {}).items():
            for other, other_spec in SCHEMA.items():
                if other_spec["table"] == ref_table:
                    targets[collection].add(other)
    ordered: list[str] = []
    remaining = set(SCHEMA)
    while remaining:
        ready = [c for c in remaining if not (targets[c] & remaining)]
        if not ready:  # cycle guard (shouldn't happen with this schema)
            ready = sorted(remaining)
        ordered.extend(sorted(ready))
        remaining -= set(ready)
    return ordered