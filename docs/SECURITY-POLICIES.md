# Hard Security Policies — the layer above the hierarchy

> **HARD SECURITY POLICIES ALWAYS OVERRIDE THE HIERARCHY.**

The org chart grants agents *authority*. Hard security policies *bound* it.
A policy is a declarative rule evaluated on **every tool call BEFORE the
agent's own permissions**, so no agent — including the CEO — can grant itself
something a policy forbids. A senior agent cannot override:

- platform security policies
- credential isolation
- prohibited actions (e.g. production DB writes)
- human-required approvals (e.g. production deploys)
- the emergency stop

## How enforcement works

```
agent tool call
   │
   ▼
PolicyEngine.evaluate()          ← FIRST (this page)
   │  deny            → refused, audited, reason returned
   │  require_approval → forced human approval gate
   │  restrict_scope  → call coerced into a narrower permission scope
   │  emergency       → EVERYTHING refused until a human stands down
   │  allow
   ▼
Permission policy (agent's allow/deny map)   ← SECOND
   ▼
Approval gate (risk level)                    ← THIRD
   ▼
handler runs, audit-logged
```

The evaluation order inside the engine is: **emergency → deny → restrict →
approve**. A deny always wins, even over a `require_approval` policy.

## Built-in defaults

Shipped in `agentos/security/policy.py`, enforced in **production**:

| Policy | Kind | Effect |
|---|---|---|
| `prod-deploy-human-approval` | require_approval | Deploys in production always need a human, even if the agent holds the `deploy` permission |
| `prod-db-writes-forbidden` | deny_tool | `postgres.query` calls in production whose args contain `insert/update/delete/drop/alter/truncate/grant/revoke/create` are refused outright |
| `no-credential-extraction` | deny_tool | `shell`/`api.call`/`web.scrape`/`github` in production may not carry credential-shaped arguments |

Enabled from `config/policies.yaml` (can be extended there):

| Policy | Kind | Effect |
|---|---|---|
| `browser-never-authenticate` | deny_tool | Agents may research with the browser but never drive login / sign-in / password / checkout / payment / admin flows (`browser.click` / `browser.type` / `browser.evaluate`) |
| `destructive-git-human-approval` | require_approval | Force-pushing or rewriting shared git history via `github` always needs a human — even for the CTO |
| `prod-api-readonly` | restrict_scope | Production `api.call` invocations are coerced into the read-only scope |

They are deliberately gated to `production` so development/offline work is
unaffected. They cannot be changed at runtime.

## Adding policies — `config/policies.yaml` (three are already enabled by default)

Extend the platform with your own immutable rules:

```yaml
security_policies:
  - id: destructive-git-human-approval
    kind: require_approval
    description: Force-pushing or rewriting shared git history requires human approval.
    tools: [github]
    arg_pattern: "(?i)(force|push --force|reset --hard|rebase)"
    approval_risk: high
    reason: rewriting shared git history requires human approval

# Deliberately disable a baked-in default (logged loudly at boot):
# disabled: [prod-db-writes-forbidden]
```

Policy fields:

| Field | Meaning |
|---|---|
| `id` | unique identifier |
| `kind` | `deny_tool` \| `require_approval` \| `restrict_scope` |
| `tools` | tool names (empty = all tools) |
| `agents` / `roles` | who it applies to (empty = everyone) |
| `environments` | `development` / `production` (empty = everywhere) |
| `arg_pattern` | regex matched against serialized args (optional) |
| `reason` | returned to the agent + audit log when the policy fires |
| `approval_risk` | risk level of the forced approval |
| `scope` | coerced permission scope for `restrict_scope` |

## Emergency stop (human-only)

Engaged, **every tool call in the system is refused** — no agent can lift it;
only an operator (human) can stand it down. The state persists across restarts.

```bash
agent-os emergency status            # Green / 🚨 ENGAGED
agent-os emergency engage "incident" # halt everything
agent-os emergency disengage         # resume tool execution
```

In Mattermost:

```
@agent emergency server breach detected   → 🚨 everything halted
@agent stand-down                         → ✅ resumed
@agent policies                           → list the active hard policies
```

Every engage/disengage is audit-logged and published as
`security.emergency_engaged` / `security.emergency_disengaged` (critical
severity).

## Operator views

```bash
agent-os policy list        # all active policies
agent-os policy evaluate --agent cto --tool postgres.query \
         --args '{"query":"UPDATE users SET x=1"}' --env production
```

API: `GET/POST/DELETE /api/security/emergency`, `GET /api/security/policies`.

## Design notes

- Policies are **immutable at runtime**: only the emergency flag is a live
  control, and only via the operator paths (API/CLI/Mattermost human
  commands) — never through the tool layer.
- Enforcement lives in the single execution path (`ToolExecutor`), so every
  tool (built-in, MCP, capability, browser) is covered by construction.
- Policy decisions are audit-logged (`tool.denied` with the policy id,
  `tool.emergency_blocked`).
- Tests: `tests/test_security_policy.py` proves a policy blocks an agent who
  *holds* the permission, that the emergency stop refuses everything and
  recovers, and that state survives engine restarts.