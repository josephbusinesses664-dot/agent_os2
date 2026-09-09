# Agent OS — the Unified AI Agency / Agent Operating System

> **📍 Project home moved:** this project now lives at
> **[agent_os2](https://github.com/josephbusinesses664-dot/agent_os2)** —
> the new canonical repository. All new development, upgrades and features
> land there. This repo (agent-os) is legacy. See [NOTES.md](NOTES.md).

A multi-agent platform where you talk to an entire organization of AI agents
through **Mattermost**. Agents collaborate, delegate, spawn sub-agents, use
skills and MCP tools, route work to the right models, respect budgets, ask
for human approval on dangerous actions, and keep full audit trails.

Built as one coherent operating system — not ten repos glued together:
LangGraph orchestration, PostgreSQL + Redis persistence, a pluggable model
router (Claude / DeepSeek / GLM / local / offline echo), a skill registry
with progressive disclosure, an MCP registry, structured agent-to-agent
messaging, an approval system, a CLI, and a web admin UI.

> 🧪 **Works fully offline.** With no API keys set, the built-in `echo`
> provider runs the entire pipeline (demo included) so you can see and test
> everything before adding keys.

---

## Quickstart

```bash
# 1. install
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. run the offline end-to-end demo (no keys needed)
agent-os demo

# 3. start the full system
agent-os start          # API + worker + Mattermost listener
# open the admin UI at http://localhost:8300

# 4. check everything
agent-os health
agent-os status
```

With Docker:

```bash
cp .env.example .env     # add keys
docker compose up --build -d
```

With Mattermost (see [docs/MATTERMOST.md](docs/MATTERMOST.md)): set
`MATTERMOST_URL` + `MATTERMOST_TOKEN`, start the system, and message the bot
in `town-square`:

```
I want to build a new SaaS product.            → executive creates a project
@agent status                                   → live agent status board
@agent approve apr_xxx                          → approve a high-risk action
@agent stop cto / @agent resume cto             → human override
```

---

## What's inside

| Subsystem | Where | Notes |
|---|---|---|
| Control plane API + admin UI | `agentos/api/`, `agentos/admin/` | FastAPI, single-file SPA, opt-in Bearer auth (`API_AUTH_TOKEN`), mission timeline per project, admin views for security/emergency, workspaces, mission replay |
| Orchestration | `agentos/orchestration/` | LangGraph state machine, **durable checkpoints** (entity-store-backed — runs survive restarts, see `orchestration/checkpoints.py`), retries, approval gates |
| Agent runtime | `agentos/agents/` | observe → plan → act → **verify** → recover loop, inbox-fed context, auto memory, tracing, per-run browser session |
| Agent registry | `agentos/registries/agent_registry.py` | 41 agents in a hierarchical org chart |
| Skill registry | `agentos/registries/skill_registry.py` + `skills/` | 100+ skills, 20 branches, progressive loading |
| Capabilities | `agentos/capabilities/` | skills become executable: tools + hooks + validators + tests (sandboxed inline code) |
| Dynamic planning | `agentos/planning.py` + `workflows/stage_templates.yaml` | the executive plans required stages per goal; simple requests never run the full pipeline |
| Model layer | `agentos/models/` | providers (Claude/DeepSeek/GLM/OpenAI-compat/echo), router, failover, **performance-aware routing**, **circuit breaker** (consecutive failures open a model's circuit, routed around until cooldown) |
| Budgets | `agentos/budgets/` | global/project/agent/task limits, auto-downgrade |
| Tools + MCP | `agentos/tools/`, `agentos/registries/mcp_registry.py` | capability discovery, **default-deny permissions**, strategy-change retries, timeouts, health checks, MCP lifecycle + credential isolation |
| Hard security policies | `agentos/security/policy.py` + `config/policies.yaml` | **policies always override the hierarchy**: evaluated before agent permissions (deny / forced human approval / scope coercion), emergency stop that refuses every tool call until a human stands down |
| Isolated workspaces | `agentos/workspaces/` | per-task **git worktrees** for parallel engineering: isolate → work → integrate (merge back) → discard, a coding-harness interface for future runtimes, and `WORKSPACE_ISOLATION=true` to run coding stages inside worktrees from the orchestrator |
| Browser | `agentos/integrations/browser.py` | Playwright-powered automation (open/snapshot/click/type/evaluate/screenshot) through the executor + permission system |
| Adapters | `agentos/tools/builtin.py` | read-only GitHub, Postgres, Docker adapters (technically enforced, approval-gated) |
| Memory | `agentos/memory/` | layered memory (task/project/agent/org/user) with TF-IDF semantic recall, **knowledge-graph links**, **contradiction resolution**, versioned + temporal facts, provenance, consolidation |
| Messaging | `agentos/messaging/` | structured agent-to-agent messages: handoffs, blockers, challenges, escalation |
| Performance | `agentos/performance.py` | operational stats that shape routing and delegation; **downstream-success tracking** (not cosmetic XP) |
| Evaluation | `agentos/evaluation/` + `eval_sets/` | deterministic + LLM-judge scoring, regression datasets (incl. `churchapp` — the 9-stage Autonomous Company Benchmark), benchmark runner, leaderboards, **failure analysis → recommendations**, capability comparison |
| Tracing | `agentos/observability/trace.py` | span chains agent → stage → model → tool → evaluator |
| Projects & tasks | `agentos/projects/`, `agentos/tasks/` | dependency graphs, auto-unblocking |
| Security | `agentos/security/` | permissions, scopes, approvals, audit log, secret redaction |
| Mattermost | `agentos/integrations/mattermost/` | identity layer, workspace org, commands |
| CLI | `agentos/cli/main.py` | `agent-os …` |
| Workflows | `workflows/*.yaml` | declarative 0→100, discovery, build-feature, **agency-loop** |
| Prompts | `prompts/*.md` | versioned, composable role prompts |

## Key commands

```bash
agent-os start | stop | status | health
agent-os agents list | create | disable | enable | show
agent-os skills list | search | enable | disable | reload
agent-os projects list | create | show
agent-os tasks list | create | show | retry
agent-os models list
agent-os budget status
agent-os mcp list | register
agent-os apis list
agent-os workflows list | run
agent-os approvals list | approve | reject
agent-os policy list | evaluate     # hard security policies (above the hierarchy)
agent-os emergency status | engage | disengage   # human-only global stop
agent-os workspace list | isolate | integrate | discard   # isolated worktrees
agent-os schedule list | create | pause | resume | run   # recurring/standing missions
agent-os events | audit | memory
agent-os evaluate basic       # benchmark a regression dataset
agent-os evaluate churchapp   # Autonomous Company Benchmark (9-stage ChurchApp mission)
agent-os leaderboard          # agent performance leaderboard
agent-os traces [task_id]     # span chain for a task
agent-os consolidate          # memory consolidation
agent-os plan "goal"          # show the dynamic stage plan for a goal
agent-os plan "goal" --execute  # run exactly the planned stages
agent-os analyze [goal]       # failure analysis + who-does-this-best
agent-os ask "your goal"      # executive flow
agent-os demo                 # offline end-to-end demo
```

## Live-run reliability fixes (AGENT-DEBRIEF.md → code)

Fixes from the Mattermost run debrief are now in the platform:

- **Tool aliases + closest-match errors** — models that guess
  `project_state` / `repo_tree` / `arch.tree` / `git_status` get the real
  tool; unknown tools reply `did you mean …?` so the next call works.
- **Fail-closed executive GO/NO-GO gate** — the gate now reviews the
  research stage that *just finished* (it previously read stale state), and
  returns NO-GO instead of blindly GO when the executive model is
  unreachable or research produced no evidence.
- **Guaranteed stage artifacts** — every stage template's planned
  deliverable (`artifacts/*.md`) lands on disk even when the model never
  issues the write; stub files are replaced with the real output.
- **Per-project agent inboxes** — an agent working project A never sees
  handoffs from sibling project B (no cross-run state confusion).
- **Worker duplicate-task guard** — queued tasks that are already
  running/queued/completed are skipped, so worker loops cannot resurrect or
  duplicate stage executions.
- **Reddit scraping via old.reddit JSON** — `web.scrape` on reddit.com
  returns real post text instead of the JS-rendered shell.

## Dynamic planning

Simple requests do **not** blindly run every stage. The executive plans the
required stages from the goal (`understanding → … → report`, with only the
relevant middle stages) — a one-line README runs 5 stages, while a major
product launch still expands to the full pipeline. Override with a
`PLANNED_STAGES: [research, report]` marker in the goal, or `--full` for the
complete 0→100 set.

```bash
agent-os plan "fix the login button"
# Stages (5): understanding → implement-frontend → implement-backend → testing → report

agent-os plan "launch a full saas product" --full
agent-os plan "research the market" --execute
```

## The 0 → 100 workflow

`agent-os ask "I want to build a SaaS product"` runs:

```
understanding → discovery → community intelligence → opportunity → strategy →
product definition → PRD → design → architecture → implementation → testing →
security → performance → final review → deployment (⚠️ human approval) → monitoring
```

Every stage is executed by the appropriate agent, artifacts land in the
project's sandboxed workspace, and everything is visible in Mattermost, the
admin UI, the event stream and the audit log. The admin UI also ships
**Security** (hard policies + human-only emergency stop), **Mission Timeline**
(chronological replay of any project's events/tasks/approvals/messages — a
failed run is reconstructable after the fact) and **Workspaces** (isolate,
status, diff, integrate, discard) views.

Durability: workflows are checkpointed through the same entity store as the
rest of the organization (Postgres in production), so a run interrupted by a
restart resumes from its last checkpoint instead of starting over — see
`agentos/orchestration/checkpoints.py`. Models are protected by a circuit
breaker (`agentos/models/router.py`): `MODEL_CIRCUIT_THRESHOLD` consecutive
failures open a model's circuit and the router routes around it until the
cooldown expires.

Relational database: when `DATABASE_URL` is set, every aggregate (agents,
projects, tasks, approvals, events, audit, memory, workspaces, …) lives in
its own table with a typed primary key and **real foreign keys** enforced by
Postgres — a task referencing a nonexistent project is rejected by the
database, and legacy `agentos_docs` rows migrate automatically on startup.
See [DATABASE.md](docs/DATABASE.md).

Concurrency: workers take distributed locks (`task:<id>:exec`) against Redis
before executing a task, so two workers can never run the same task at once
— even across containers. In-process locks keep the same guarantees in
offline/tests. `LOCK_TTL_SECONDS` controls the TTL.

## Documentation

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design and data flow
- [INSTALLATION.md](docs/INSTALLATION.md) — local, Docker, and production notes
- [CONFIGURATION.md](docs/CONFIGURATION.md) — every env var explained
- [DATABASE.md](docs/DATABASE.md) — the relational Postgres schema and distributed locks
- [AGENTS.md](docs/AGENTS.md) — the org chart, permissions, spawning
- [SKILLS.md](docs/SKILLS.md) — skill architecture and progressive disclosure
- [MCP.md](docs/MCP.md) — MCP servers, registry, permissions
- [MODELS.md](docs/MODELS.md) — tiers, routing, failover, cost control
- [MATTERMOST.md](docs/MATTERMOST.md) — wiring the human interface
- [SECURITY.md](docs/SECURITY.md) — permissions, approvals, audit
- [SECURITY-POLICIES.md](docs/SECURITY-POLICIES.md) — hard policies that override the hierarchy, emergency stop
- [WORKSPACES.md](docs/WORKSPACES.md) — isolated git-worktree engineering workspaces
- [WORKFLOWS.md](docs/WORKFLOWS.md) — declarative workflows and gates
- [OBSERVABILITY.md](docs/OBSERVABILITY.md) — events, logs, budgets
- [ADMIN-UI.md](docs/ADMIN-UI.md) — the web console
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — extending the system
- [TESTING.md](docs/TESTING.md) — running the test suite
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common problems

## License

MIT — see [LICENSE](LICENSE). Skills adapted from the approved capability
sources (Superpowers, Addy Osmani code review, MengTo design methodology,
GSAP official docs, Reddit research practices) carry provenance in their
frontmatter.