# Database: relational schema & distributed locks

## Relational schema (master spec §32)

PostgreSQL is the durable source of truth — and it stores the organization's
state *relationally*, not as opaque JSON blobs. Every aggregate gets a
dedicated table with a **typed primary key** and **real foreign keys** to the
aggregates it references, so cross-aggregate integrity is enforced by the
database rather than by application discipline.

| Collection | Table | Keys | Foreign keys |
|---|---|---|---|
| agents | `agents` | `id` | — |
| agent_instances | `agent_instances` | `agent_id` | → agents |
| projects | `projects` | `project_id` | — |
| tasks | `tasks` | `task_id` | → projects, → agents (assignee) |
| skills / tools / mcp / models / apis | `skills` `tools` `mcp` `models` `apis` | id/name/api | — |
| events | `events` | `event_id` | → projects, → agents |
| audit | `audit` | `audit_id` | → projects, → tasks, → agents |
| memory / memory_links | `memory` `memory_links` | memory_id/link_id | — |
| approvals | `approvals` | `approval_id` | → projects, → tasks, → agents |
| budgets | `budgets` | `(scope, scope_id)` composite | — |
| messages | `messages` | `message_id` | → projects |
| decisions | `decisions` | `decision_id` | → projects |
| evaluations / eval_runs / traces / workflows | `evaluations` `eval_runs` `traces` `workflows` | ids | — |
| performance | `performance` | `(agent_id, window)` | → agents |
| workspaces | `workspaces` | `workspace_id` | → projects, → tasks, → agents |
| checkpoints | `checkpoints` | `(thread_id, checkpoint_ns, checkpoint_id)` | — |
| checkpoint_latest | `checkpoint_latest` | `thread_id` | — |
| policy_control | `policy_control` | `key` | — |

Design notes:

- **Hybrid payload**: each table keeps the full aggregate in a JSONB `data`
  column (cheap schema evolution), while identity columns are typed and
  references are enforced FKs. This is the standard production pattern —
  relational integrity where it matters, flexible payloads alongside.
- **`ON DELETE RESTRICT`** (default): the database refuses to delete a
  project that still has tasks, an agent that still has approvals, etc.
  Orphaning is impossible.
- **Optional references** are stored as `NULL` (empty-string ids in legacy
  models map to NULL at write time), so a phantom `""` never trips an FK.
- **Migration**: the previous deployment stored everything in a single
  `agentos_docs` JSONB table. On startup (`init()`), the relational
  repository creates the typed tables and idempotently migrates legacy rows
  into them (`INSERT ... ON CONFLICT DO NOTHING`). Rows that reference
  deleted parents (orphans) are logged and skipped — the migration never
  bricks on dirty data. Collections without a typed table still fall back to
  `agentos_docs`.
- **`Repository` interface preserved**: business code talks to the same
  put/get/query contract whether the backend is in-memory, SQLite (tests),
  or PostgreSQL (production). The relational repository is selected
  automatically when `DATABASE_URL` is set.

## Distributed locks (master spec §34)

Many agents operate simultaneously — the box runs an app container *and* a
worker container against one Redis. Without arbitration, two workers can race
the same task: double execution, double spending, duplicate deployments.

`agentos/db/locks.py` provides a `LockManager`:

- `acquire(key, ttl, owner)` — `SET key owner NX PX ttl` (atomic: one winner).
- `release(key, owner)` — Lua compare-and-delete (only the owner releases).
- `is_locked(key)` — existence check (TTL-expiring).
- In-memory fallback with identical semantics when no Redis is configured
  (offline/tests), so concurrency bugs surface in CI without a live server.

The worker loop takes `task:<id>:exec` before running a task and releases it
afterwards, so **two workers can never execute the same task at once** — even
across processes. The existing DB-status check stays as a cheap second guard;
the lock is the arbiter. If a worker dies mid-task, the lock TTL expires and
the next worker picks the task up (no permanent stall). `LOCK_TTL_SECONDS`
controls the TTL.