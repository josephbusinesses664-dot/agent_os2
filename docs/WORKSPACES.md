# Isolated Engineering Workspaces (parallel software engineering)

Many agents can work on the same project **at the same time** without
clobbering each other. Every coding task gets its own isolated workspace — a
real **git worktree** cut from the project repository when one exists, or a
plain sandbox directory when it doesn't — and the finished work is merged
back through the normal review → test → merge path.

```
Backend Worker ──▶ worktree: agentos/backend  ──▶ integrate() ──┐
Frontend Worker ─▶ worktree: agentos/frontend ──▶ integrate() ──┼──▶ main
Security Worker ─▶ worktree: agentos/security ──▶ integrate() ──┘
```

## Lifecycle

```
isolate()   create the worktree / sandbox dir and record it
status()    git status inside the workspace (dirty files, commits)
diff()      git diff against the base branch
integrate() commit the work, merge it back to the base branch
discard()   remove the worktree + branch (or the sandbox dir)
```

`integrate()` never loses work:

- uncommitted changes are committed with the agent's attribution,
- the branch is merged with `--no-ff` so the workspace is always traceable,
- a conflict **marks the workspace failed but preserves the branch and its
  commits** for a lead/human to resolve — nothing is destroyed.

If the project directory is not a git repository (or git worktrees are
unavailable), the workspace degrades to a plain isolated directory — still
isolated per task, just with no merge-back.

## Operator commands

```bash
agent-os workspace list                                  # all workspaces
agent-os workspace isolate --project <id> --task <id> --agent <id> [--repo <path>]
agent-os workspace show <ws_id>
agent-os workspace status <ws_id>                        # dirty files, commits
agent-os workspace diff <ws_id>
agent-os workspace integrate <ws_id>                     # commit + merge back
agent-os workspace discard <ws_id>
```

API: `GET/POST /api/workspaces`, `GET /api/workspaces/{id}`,
`GET .../status`, `GET .../diff`, `POST .../integrate`, `POST .../discard`.

## The coding harness interface

`agentos/workspaces/harness.py` defines the `CodingHarness` contract the
organizational layer uses for any coding runtime:

```
prepare(svc, agent, task, project)   → isolated workspace
execute(svc, agent, task, project)   → run the work inside the workspace
verify(svc, workspace, command)      → status + optional test command
integrate(svc, workspace)            → merge back
```

`LocalCodingHarness` is the built-in implementation: it runs the agent
runtime with the **RuntimeContext workspace pointed at the worktree path**, so
every filesystem write lands in the isolated tree, then integrates. Future
runtimes (OpenCode / OpenHands / Claude Code / Codex connectors) implement
the same protocol — the OS never depends on *how* a workspace is created.

```python
result = await svc.coding.execute(svc, agent, task, project, repo_path=...)
# result.merge == {"ok": True, "merged": True, "commits": [...], ...}
```

The harness is exposed as `svc.coding` on the Services bundle; the
orchestrator's default stage path is unchanged (workspaces are opt-in per
task), so existing workflows keep their semantics.

## Tests

`tests/test_workspaces.py` uses **real git repositories** and verifies:

- worktree isolation + merge-back into the base repo,
- two parallel worktrees editing different files merge cleanly,
- a conflicting merge marks the workspace failed without losing either side's
  work,
- discard removes the worktree and branch,
- the harness runs a full agent task inside the worktree end to end.